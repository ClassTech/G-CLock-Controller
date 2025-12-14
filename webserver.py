# File: webserver.py (optimized)
# Compile with: mpy-cross -O2 webserver.py

import usocket as socket
import ujson
import time
import machine
import ucollections
import gc

# Pre-allocate common responses with CORS headers
HTTP_200_JSON = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\nConnection: close\r\n\r\n'
HTTP_200_HTML = b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n'
HTTP_404 = b'HTTP/1.1 404 Not Found\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\nNot Found'
HTTP_400 = b'HTTP/1.1 400 Bad Request\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\nBad Request'
HTTP_OPTIONS = b'HTTP/1.1 200 OK\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\nConnection: close\r\n\r\n'
OK_RESPONSE = b'{"status":"ok"}'

def _parse_json_body(client, headers_raw):
    """Parse JSON body from request with error handling"""
    try:
        headers = headers_raw.split(b'\r\n')
        content_length_header = [h for h in headers if h.lower().startswith(b'content-length:')]
        if not content_length_header: 
            return None
        content_length = int(content_length_header[0].split(b': ')[1])
        body = client.recv(content_length)
        return ujson.loads(body)
    except:
        return None

def send_response(client, body, content_type="application/json"):
    """Send HTTP response efficiently"""
    if content_type == "application/json":
        client.send(HTTP_200_JSON)
    else:
        client.send(HTTP_200_HTML)
    
    if isinstance(body, bytes): 
        client.sendall(body)
    else: 
        client.sendall(body.encode('utf-8'))

def send_json_ok(client):
    """Send simple OK JSON response"""
    client.send(HTTP_200_JSON)
    client.send(OK_RESPONSE)

# --- Request Handlers ---
def serve_static_file(client, file_path, content_type):
    """Serve static files with chunked reading"""
    try:
        if content_type == "text/html":
            client.send(HTTP_200_HTML)
        else:
            client.send(f'HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nConnection: close\r\n\r\n'.encode('utf-8'))
        
        with open(file_path, 'r') as f:
            while True:
                chunk = f.read(512)  # Read in small chunks
                if not chunk: 
                    break
                client.sendall(chunk.encode('utf-8'))
    except OSError:
        client.send(HTTP_404)

def handle_status_request(client, pendulum_state, **kwargs):
    """Return essential system status - reduced data to save memory"""
    try:
        # Only send essential fields to reduce memory usage
        essential_state = {
            'currPosIn': pendulum_state.get('currPosIn', 0.0),
            'swingCount': pendulum_state.get('swingCount', 0),
            'avgBeatTime': pendulum_state.get('avgBeatTime', 0.0),
            'totalDriftS': pendulum_state.get('totalDriftS', 0.0),
            'correctionActive': pendulum_state.get('correctionActive', True),
            'tickTockDiff': pendulum_state.get('tickTockDiff', 0.0),
            'elapsedTimeStr': pendulum_state.get('elapsedTimeStr', '0d 0h 0m 0s'),
            'driftSph': pendulum_state.get('driftSph', 0.0),
            'missedBeats': pendulum_state.get('missedBeats', 0),
            'kp': pendulum_state.get('kp', 0.0002),
            'ki': pendulum_state.get('ki', 0.00002),
            'lastRateError': pendulum_state.get('lastRateError', 0.0)
        }
        
        json_response = ujson.dumps(essential_state)
        send_response(client, json_response)
    except Exception as e:
        # Log error and send empty response
        print(f"Status request error: {e}")
        send_response(client, '{"error":"status_failed"}')

def handle_log_request(client, log_buffer, **kwargs):
    """Stream log entries efficiently without building large string"""
    client.send(HTTP_200_JSON)
    client.send(b'[')
    
    first = True
    for entry in log_buffer:
        if not first: 
            client.send(b',')
        client.send(ujson.dumps(entry).encode('utf-8'))
        first = False
    
    client.send(b']')

def handle_history_request(client, pendulum_state, **kwargs):
    """Return hourly history data"""
    history_list = list(pendulum_state.get("hourlyHistory", []))
    send_response(client, ujson.dumps(history_list))

def handle_file_upload(client, headers_raw, log_func, **kwargs):
    """Handle file uploads with streaming"""
    headers = headers_raw.split(b'\r\n')
    path_header = [h for h in headers if h.lower().startswith(b'x-target-path:')]
    if not path_header:
        log_func("UPLOAD: Missing X-Target-Path header.")
        client.send(HTTP_400)
        return
        
    target_path = path_header[0].split(b': ')[1].decode('utf-8').strip()
    if target_path in ('/boot.py',):
        client.send(HTTP_400)
        return
        
    try:
        content_length_header = [h for h in headers if h.lower().startswith(b'content-length:')]
        content_length = int(content_length_header[0].split(b': ')[1])
        
        with open(target_path, 'wb') as f:
            bytes_written = 0
            while bytes_written < content_length:
                chunk_size = min(512, content_length - bytes_written)
                chunk = client.recv(chunk_size)
                if not chunk: 
                    break
                f.write(chunk)
                bytes_written += len(chunk)
                
        log_func(f"UPLOAD: Wrote {bytes_written} bytes to {target_path}")
        send_json_ok(client)
    except Exception as e:
        log_func(f"UPLOAD: Error writing file: {e}")
        client.send(HTTP_400)

def handle_restart_request(client, log_func, **kwargs):
    """Handle system restart request"""
    log_func("CONTROL: Restart requested...")
    send_json_ok(client)
    time.sleep(2)
    machine.reset()

def handle_move_request(client, headers_raw, pendulum_state, log_func, **kwargs):
    """Handle manual move requests"""
    data = _parse_json_body(client, headers_raw)
    if data and "inches" in data:
        inches = float(data["inches"])
        pendulum_state["moveRequest"] += inches
        log_func(f"Web request for move: {inches:.6f} inches")
        send_json_ok(client)
    else: 
        client.send(HTTP_400)

def handle_update_tuning_request(client, headers_raw, pendulum_state, log_func, save_state_func, **kwargs):
    """Handle PID tuning parameter updates"""
    data = _parse_json_body(client, headers_raw)
    if not data: 
        client.send(HTTP_400)
        return
    
    updated = False
    if "kp" in data:
        new_kp = float(data["kp"])
        pendulum_state["kp"] = new_kp
        log_func(f"TUNING: Kp updated to {new_kp:.6f}")
        updated = True
        
    if "ki" in data:
        new_ki = float(data["ki"])
        pendulum_state["ki"] = new_ki
        log_func(f"TUNING: Ki updated to {new_ki:.6f}")
        updated = True
    
    if updated:
        save_state_func()
        
    send_json_ok(client)

def handle_toggle_corrections_request(client, headers_raw, pendulum_state, **kwargs):
    """Toggle automatic corrections on/off"""
    data = _parse_json_body(client, headers_raw)
    if data and "active" in data:
        pendulum_state["correctionActive"] = bool(data["active"])
        send_json_ok(client)
    else: 
        client.send(HTTP_400)

def handle_set_zero_request(client, pendulum_state, log_func, save_state_func, **kwargs):
    """Reset position reference to zero"""
    pendulum_state["currPosIn"] = 0.0
    log_func("ZERO SET: Position reference has been reset.")
    save_state_func()
    send_json_ok(client)

def handle_options_request(client, **kwargs):
    """Handle CORS preflight requests"""
    client.send(HTTP_OPTIONS)

def handle_debug_request(client, **kwargs):
    """Simple debug endpoint"""
    debug_info = {
        "status": "ok",
        "time": time.time(),
        "memory": gc.mem_free() if hasattr(gc, 'mem_free') else "unknown"
    }
    send_response(client, ujson.dumps(debug_info))
    """Handle CORS preflight requests"""
    client.send(HTTP_OPTIONS)
def handle_reset_timing_request(client, pendulum_state, log_func, save_state_func, **kwargs):
    """Reset timing measurement period"""
    now = time.time()
    pendulum_state.update({
        "timingStartUtc": now, 
        "swingCount": 0, 
        "totalDriftS": 0.0,
        "lastCorrectionUtc": now, 
        "hourlyHistory": ucollections.deque((), 168)
    })
    log_func("TIMING RESET: New period started by user.")
    save_state_func()
    send_json_ok(client)

# --- Core Server Logic ---
def runServer(pendulum_state, log_buffer, log_func, save_state_func):
    """Main web server loop with memory optimization"""
    
    # Route definitions - including OPTIONS for CORS
    routes = {
        ('GET', '/'): ('static', 'index.html', 'text/html'),
        ('GET', '/debug'): ('handler', handle_debug_request),  # Debug endpoint
        ('GET', '/status'): ('handler', handle_status_request),
        ('GET', '/log'): ('handler', handle_log_request),
        ('GET', '/history'): ('handler', handle_history_request),
        ('POST', '/upload-file'): ('handler', handle_file_upload),
        ('POST', '/restart'): ('handler', handle_restart_request),
        ('POST', '/move'): ('handler', handle_move_request),
        ('POST', '/setZero'): ('handler', handle_set_zero_request),
        ('POST', '/resetTiming'): ('handler', handle_reset_timing_request),
        ('POST', '/updateTuning'): ('handler', handle_update_tuning_request),
        ('POST', '/toggleCorrections'): ('handler', handle_toggle_corrections_request),
        # Add OPTIONS handlers for CORS preflight
        ('OPTIONS', '/debug'): ('handler', handle_options_request),
        ('OPTIONS', '/status'): ('handler', handle_options_request),
        ('OPTIONS', '/log'): ('handler', handle_options_request),
        ('OPTIONS', '/history'): ('handler', handle_options_request),
        ('OPTIONS', '/move'): ('handler', handle_options_request),
        ('OPTIONS', '/setZero'): ('handler', handle_options_request),
        ('OPTIONS', '/resetTiming'): ('handler', handle_options_request),
        ('OPTIONS', '/updateTuning'): ('handler', handle_options_request),
        ('OPTIONS', '/toggleCorrections'): ('handler', handle_options_request),
    }

    # Setup server socket
    addr = socket.getaddrinfo('0.0.0.0', 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(3)  # Reduced listen queue to save memory
    log_func(f"Web server listening on {addr}")

    # Reusable context dict to avoid repeated allocations
    context = {
        'pendulum_state': pendulum_state,  # Changed key name to match handler expectations
        'log_buffer': log_buffer,
        'log_func': log_func,
        'save_state_func': save_state_func
    }

    while True:
        client = None
        try:
            client, client_addr = s.accept()
            client.settimeout(3.0)  # Shorter timeout to free up connections faster
            
            # Read request headers
            headers_raw = b""
            while True:
                line = client.readline()
                if not line or line == b'\r\n':
                    break
                headers_raw += line
            
            if not headers_raw:
                continue
            
            # Parse request line
            request_line = headers_raw.split(b'\r\n')[0]
            try:
                method, path, _ = request_line.split(b' ', 2)
                method_str = method.decode('utf-8')
                path_str = path.decode('utf-8')
            except Exception as parse_error:
                log_func(f"Request parse error: {parse_error}")
                client.send(HTTP_400)
                continue
            
            # Look up route handler
            route_key = (method_str, path_str)
            route_info = routes.get(route_key)
            
            if route_info:
                route_type, handler_or_file = route_info[0], route_info[1]
                
                if route_type == 'static':
                    # Static file serving
                    file_path, content_type = route_info[1], route_info[2]
                    serve_static_file(client, file_path, content_type)
                    
                elif route_type == 'handler':
                    # Dynamic handler function
                    handler_func = handler_or_file
                    # Add headers_raw to context for this request
                    request_context = dict(context)  # Make a copy
                    request_context['headers_raw'] = headers_raw
                    try:
                        handler_func(client, **request_context)
                    except Exception as handler_error:
                        log_func(f"Handler error for {path_str}: {handler_error}")
                        client.send(HTTP_400)
            else:
                client.send(HTTP_404)
                
        except Exception as e:
            log_func(f"Web server error: {e}")
        finally:
            if client:
                try:
                    client.close()
                except:
                    pass
            # Force garbage collection after each request to prevent memory buildup
            gc.collect()