# File: webserver.py (optimized)
# Compile with: mpy-cross -O2 webserver.py

import usocket as socket
import ujson
import time
import machine
import ucollections
import gc

HTTP_200_JSON = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\nConnection: close\r\n\r\n'
HTTP_200_HTML = b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n'
HTTP_404 = b'HTTP/1.1 404 Not Found\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\nNot Found'
HTTP_400 = b'HTTP/1.1 400 Bad Request\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\nBad Request'
HTTP_OPTIONS = b'HTTP/1.1 200 OK\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\nConnection: close\r\n\r\n'
OK_RESPONSE = b'{"status":"ok"}'

def _parse_json_body(client, headers_raw):
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
    if content_type == "application/json":
        client.send(HTTP_200_JSON)
    else:
        client.send(HTTP_200_HTML)
    
    if isinstance(body, bytes): 
        client.sendall(body)
    else: 
        client.sendall(body.encode('utf-8'))

def send_json_ok(client):
    client.send(HTTP_200_JSON)
    client.send(OK_RESPONSE)

def serve_static_file(client, file_path, content_type):
    try:
        if content_type == "text/html":
            client.send(HTTP_200_HTML)
        else:
            client.send(f'HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nConnection: close\r\n\r\n'.encode('utf-8'))
        
        with open(file_path, 'r') as f:
            while True:
                chunk = f.read(512) 
                if not chunk: 
                    break
                client.sendall(chunk.encode('utf-8'))
    except OSError:
        client.send(HTTP_404)

# --- NEW: Lightweight Beat Endpoint ---
def handle_beat_request(client, pendulum_state, **kwargs):
    """Returns just the tick count for fast polling"""
    try:
        val = pendulum_state.get('tickCount', 0)
        # Manually build JSON string to save memory/cpu
        resp = '{"t":' + str(val) + '}'
        client.send(HTTP_200_JSON)
        client.send(resp.encode('utf-8'))
    except:
        client.send(HTTP_400)

def handle_status_request(client, pendulum_state, **kwargs):
    try:
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
            'lastRateError': pendulum_state.get('lastRateError', 0.0),
            'tickCount': pendulum_state.get('tickCount', 0),
            'watchdogTriggered': pendulum_state.get('watchdogTriggered', False)
        }
        json_response = ujson.dumps(essential_state)
        send_response(client, json_response)
    except Exception as e:
        print(f"Status request error: {e}")
        send_response(client, '{"error":"status_failed"}')

def handle_log_request(client, log_buffer, **kwargs):
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
    history_list = list(pendulum_state.get("hourlyHistory", []))
    send_response(client, ujson.dumps(history_list))

def handle_file_upload(client, headers_raw, log_func, **kwargs):
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
    log_func("CONTROL: Restart requested...")
    send_json_ok(client)
    time.sleep(2)
    machine.reset()

def handle_move_request(client, headers_raw, pendulum_state, log_func, **kwargs):
    data = _parse_json_body(client, headers_raw)
    if data and "inches" in data:
        inches = float(data["inches"])
        pendulum_state["moveRequest"] += inches
        log_func(f"Web request for move: {inches:.6f} inches")
        send_json_ok(client)
    else: 
        client.send(HTTP_400)

def handle_update_tuning_request(client, headers_raw, pendulum_state, log_func, save_state_func, **kwargs):
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
    data = _parse_json_body(client, headers_raw)
    if data and "active" in data:
        pendulum_state["correctionActive"] = bool(data["active"])
        send_json_ok(client)
    else: 
        client.send(HTTP_400)

def handle_set_zero_request(client, pendulum_state, log_func, save_state_func, **kwargs):
    pendulum_state["currPosIn"] = 0.0
    log_func("ZERO SET: Position reference has been reset.")
    save_state_func()
    send_json_ok(client)

def handle_options_request(client, **kwargs):
    client.send(HTTP_OPTIONS)

def handle_debug_request(client, **kwargs):
    debug_info = {
        "status": "ok",
        "time": time.time(),
        "memory": gc.mem_free() if hasattr(gc, 'mem_free') else "unknown"
    }
    send_response(client, ujson.dumps(debug_info))

def handle_reset_timing_request(client, pendulum_state, log_func, save_state_func, **kwargs):
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

def runServer(pendulum_state, log_buffer, log_func, save_state_func):
    routes = {
        ('GET', '/'): ('static', 'index.html', 'text/html'),
        ('GET', '/debug'): ('handler', handle_debug_request),
        ('GET', '/beat'): ('handler', handle_beat_request), # NEW ROUTE
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

    addr = socket.getaddrinfo('0.0.0.0', 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(3)
    log_func(f"Web server listening on {addr}")

    context = {
        'pendulum_state': pendulum_state,
        'log_buffer': log_buffer,
        'log_func': log_func,
        'save_state_func': save_state_func
    }

    # FIX: Track last IP to avoid console spam
    last_client_ip = None

    while True:
        client = None
        try:
            client, client_addr = s.accept()
            
            # FIX: Only print if the IP address changes
            current_ip = client_addr[0]
            if current_ip != last_client_ip:
                print(f"New Web Client: {current_ip}")
                last_client_ip = current_ip
            
            client.settimeout(3.0)
            
            headers_raw = b""
            while True:
                line = client.readline()
                if not line or line == b'\r\n':
                    break
                headers_raw += line
            
            if not headers_raw:
                continue
            
            request_line = headers_raw.split(b'\r\n')[0]
            try:
                method, path, _ = request_line.split(b' ', 2)
                method_str = method.decode('utf-8')
                path_str = path.decode('utf-8')
            except Exception as parse_error:
                log_func(f"Request parse error: {parse_error}")
                client.send(HTTP_400)
                continue
            
            route_key = (method_str, path_str)
            route_info = routes.get(route_key)
            
            if route_info:
                route_type, handler_or_file = route_info[0], route_info[1]
                
                if route_type == 'static':
                    file_path, content_type = route_info[1], route_info[2]
                    serve_static_file(client, file_path, content_type)
                    
                elif route_type == 'handler':
                    handler_func = handler_or_file
                    request_context = dict(context)
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
            gc.collect()