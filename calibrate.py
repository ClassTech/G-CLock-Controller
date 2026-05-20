# calibrate.py
# Pendulum clip calibration tool.
# Steps clip down at a configurable interval, records beats and average
# beat duration at each position. Web UI graphs results against position.
# Deploy alongside wifi.config and wifi_manager.py, then: import calibrate

import _thread, time, socket, ujson, gc
from machine import Pin
import ucollections

# ── Hardware (same as pendulum_controller) ────────────────────────────────
STEPPER_PINS   = [Pin(3,Pin.OUT), Pin(4,Pin.OUT), Pin(5,Pin.OUT), Pin(6,Pin.OUT)]
STEPS_PER_INCH = 6400
STEP_DELAY_MS  = 10
STEP_SEQUENCE  = [
    [1,0,0,0],[1,0,1,0],[0,0,1,0],[0,1,1,0],
    [0,1,0,0],[0,1,0,1],[0,0,0,1],[1,0,0,1],
]
IR_PIN       = 21
DEBOUNCE_US  = 400_000

# ── Shared state ──────────────────────────────────────────────────────────
cal = {
    "mode":         "idle",  # idle | counting | moving
    "position":     0.0,     # inches from zero
    "stepSize":     0.03125, # inches per auto-step down (one full turn of 10-32 screw)
    "windowSecs":   3600,    # measurement window duration (seconds)
    "windowStart":  0,
    "windowBeats":  0,
    "beatSum":      0.0,
    "lastBeatUs":   0,
    "seqPos":       0,
    "stepsLeft":    0,
    "stepDir":      1,
    "measurements": [],      # [{pos, beats, avgBeat}]
    "numSteps":     0,       # total measurements to take (0 = unlimited)
    "stepsDone":    0,
    "lastDirection": "",     # "up" | "down" — persists after move completes
    "log":          [],
}
beat_queue = ucollections.deque((), 20)

# ── IR interrupt ──────────────────────────────────────────────────────────
_ir = Pin(IR_PIN, Pin.IN, Pin.PULL_UP)

def _ir_handler(pin):
    try: beat_queue.append(time.ticks_us())
    except: pass

_ir.irq(trigger=Pin.IRQ_FALLING, handler=_ir_handler)

# ── Logging ───────────────────────────────────────────────────────────────
def log(msg):
    t = time.localtime()
    e = "[{:02}:{:02}:{:02}] {}".format(t[3], t[4], t[5], msg)
    print(e)
    cal["log"].append(e)
    if len(cal["log"]) > 60:
        cal["log"].pop(0)

# ── Beat processing ───────────────────────────────────────────────────────
def process_beats():
    while len(beat_queue):
        t = beat_queue.popleft()
        if cal["lastBeatUs"] == 0:
            cal["lastBeatUs"] = t
            continue
        iv = time.ticks_diff(t, cal["lastBeatUs"])
        if iv < DEBOUNCE_US:
            continue
        s = iv / 1_000_000.0
        if 0.5 < s < 1.5:
            cal["lastBeatUs"] = t
            if cal["mode"] == "counting":
                cal["windowBeats"] += 1
                cal["beatSum"]     += s

# ── Motor ─────────────────────────────────────────────────────────────────
def step_once():
    if cal["stepsLeft"] <= 0:
        return False
    p0,p1,p2,p3 = STEPPER_PINS
    pos = (cal["seqPos"] + cal["stepDir"]) % 8
    sv  = STEP_SEQUENCE[pos]
    p0.value(sv[0]); p1.value(sv[1]); p2.value(sv[2]); p3.value(sv[3])
    cal["seqPos"]    = pos
    cal["stepsLeft"] -= 1
    if cal["stepsLeft"] <= 0:
        p0.value(1); p1.value(1); p2.value(1); p3.value(1)
        time.sleep_ms(10)
        p0.value(0); p1.value(0); p2.value(0); p3.value(0)
        log("Move complete. Position: {:.4f} in".format(cal["position"]))
    return cal["stepsLeft"] > 0

def queue_move(inches):
    cal["stepDir"]   = -1 if inches > 0 else 1
    cal["stepsLeft"] = abs(int(inches * STEPS_PER_INCH))
    cal["position"] += inches

# ── State machine ─────────────────────────────────────────────────────────
def start_window():
    cal["windowStart"] = time.time()
    cal["windowBeats"] = 0
    cal["beatSum"]     = 0.0
    cal["lastBeatUs"]  = 0
    cal["mode"]        = "counting"
    log("Window started at {:.4f} in  ({} s)".format(cal["position"], cal["windowSecs"]))

def finish_window():
    n   = cal["windowBeats"]
    avg = (cal["beatSum"] / n) if n else 0.0
    cal["measurements"].append({"pos": cal["position"], "beats": n, "avgBeat": avg})
    cal["stepsDone"] += 1
    log("Done {}/{}: pos={:.4f}  beats={}  avg={:.4f}s".format(
        cal["stepsDone"], cal["numSteps"] or '?', cal["position"], n, avg))
    t = time.localtime()
    try:
        with open("calibration.csv", "a") as f:
            f.write("{:04}-{:02}-{:02} {:02}:{:02}:{:02},{:.4f},{},{:.4f}\n".format(
                t[0],t[1],t[2],t[3],t[4],t[5], cal["position"], n, avg))
    except Exception as e:
        log("CSV error: {}".format(e))
    if cal["numSteps"] > 0 and cal["stepsDone"] >= cal["numSteps"]:
        cal["mode"]      = "idle"
        cal["stepsLeft"] = 0
        log("Run complete ({} measurements).".format(cal["stepsDone"]))

# ── Main loop ─────────────────────────────────────────────────────────────
def main_loop():
    while True:
        process_beats()

        if cal["mode"] == "counting":
            if time.time() - cal["windowStart"] >= cal["windowSecs"]:
                finish_window()
                if cal["mode"] != "idle":   # finish_window may have stopped the run
                    cal["lastDirection"] = "down"
                    queue_move(-cal["stepSize"])
                    cal["mode"] = "moving"

        busy = step_once()

        if cal["mode"] == "moving" and not busy:
            start_window()

        time.sleep_ms(STEP_DELAY_MS if busy else 20)

# ── HTML page ─────────────────────────────────────────────────────────────
HTML = rb"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clip Cal</title>
<style>
*{box-sizing:border-box}
body{font:14px sans-serif;background:#1a1a2e;color:#eee;margin:10px}
h2{color:#e94560;margin:4px 0 10px}
.card{background:#16213e;padding:12px;border-radius:6px;margin-bottom:10px}
button{padding:7px 14px;border:none;border-radius:4px;cursor:pointer;color:#fff;font-size:13px;margin:3px}
.go{background:#e94560}.sec{background:#0f3460}
input[type=number]{width:72px;padding:5px;border-radius:4px;border:1px solid #444;background:#0f3460;color:#eee}
label{color:#aaa;font-size:12px;margin-right:10px}
#st{line-height:2;font-size:13px}
svg{display:block;width:100%;height:260px;background:#0f3460;border-radius:4px}
#lb{font:11px monospace;color:#7af;max-height:110px;overflow-y:auto;white-space:pre}
</style>
</head>
<body>
<h2>Clip Calibration</h2>
<div class="card" id="st">Connecting...</div>
<div class="card"><svg id="ch"></svg></div>
<div class="card">
 <button class="go" onclick="api('/start')">Start</button>
 <button class="sec" onclick="api('/stop')">Stop</button>
 <button id="bup" class="sec" onclick="api('/moveUp')">&#9650; Up</button>
 <button id="bdn" class="sec" onclick="api('/moveDown')">&#9660; Down</button>
 <br><br>
 <label>Step (in)<input type="number" id="ss" step="0.03125" value="0.03125"></label>
 <label>Window (min)<input type="number" id="wm" step="1" value="60"></label>
 <label>Num steps<input type="number" id="ns" step="1" min="0" value="0"></label>
 <button class="sec" onclick="setcfg()">Apply</button>
</div>
<div class="card" id="lb"></div>
<script>
function api(u){fetch(u,{method:'POST'}).then(refresh)}
function setcfg(){
 fetch('/config',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({step:+document.getElementById('ss').value,
   windowMins:+document.getElementById('wm').value,
   numSteps:+document.getElementById('ns').value})
 }).then(refresh)
}
function hms(s){
 return Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m '+Math.floor(s%60)+'s'
}
function refresh(){
 fetch('/status').then(function(r){return r.json()}).then(function(d){
  var rem=Math.max(0,d.windowSecs-(d.elapsed||0))
  document.getElementById('st').innerHTML=
   '<b>Mode:</b> '+d.mode+
   ' &nbsp; <b>Position:</b> '+d.position.toFixed(4)+' in<br>'+
   '<b>Beats this window:</b> '+d.windowBeats+
   ' &nbsp; <b>Beats/hr:</b> '+d.beatsPerHour.toFixed(1)+
   ' &nbsp; <b>Avg beat:</b> '+(d.avgBeat*1000).toFixed(2)+' ms<br>'+
   '<b>Elapsed:</b> '+hms(d.elapsed)+
   ' &nbsp; <b>Remaining:</b> '+hms(rem)+'<br>'+
   '<b>Step:</b> '+d.stepSize+' in'+
   ' &nbsp; <b>Window:</b> '+hms(d.windowSecs)+
   ' &nbsp; <b>Progress:</b> '+d.stepsDone+'/'+(d.numSteps||'&#8734;')
  document.getElementById('lb').textContent=d.log.slice(-20).join('\n')
  var ae=document.activeElement
  var ss=document.getElementById('ss')
  var wm=document.getElementById('wm')
  var ns=document.getElementById('ns')
  if(ae!==ss)ss.value=d.stepSize
  if(ae!==wm)wm.value=(d.windowSecs/60).toFixed(0)
  if(ae!==ns)ns.value=d.numSteps
  var up=document.getElementById('bup')
  var dn=document.getElementById('bdn')
  up.style.background=d.lastDirection==='up'?'#2e7d32':'#0f3460'
  dn.style.background=d.lastDirection==='down'?'#2e7d32':'#0f3460'
  draw(d.measurements)
 }).catch(function(e){
  document.getElementById('st').innerHTML='<span style="color:#e94560">Error: '+e+'</span>'
 })
}
function draw(data){
 var svg=document.getElementById('ch')
 var W=svg.clientWidth||500,H=260,P=45
 if(!data||!data.length){
  svg.innerHTML='<text x="50%" y="50%" fill="#888" text-anchor="middle" font-size="13">No data yet</text>'
  return
 }
 var px=data.map(function(d){return d.pos})
 var pb=data.map(function(d){return d.beats})
 var mn=Math.min,mx=Math.max
 var x0=mn.apply(null,px),x1=mx.apply(null,px)
 var b0=mn.apply(null,pb),b1=mx.apply(null,pb)
 if(x1===x0){x0-=.005;x1+=.005}
 if(b1===b0){b0-=1;b1+=1}
 var dw=W-2*P,dh=H-2*P
 function sx(v){return (P+(v-x0)/(x1-x0)*dw).toFixed(1)}
 function sy(v){return (H-P-(v-b0)/(b1-b0)*dh).toFixed(1)}
 var h=''
 for(var i=0;i<=4;i++){
  var gy=(P+dh*i/4).toFixed(0)
  var bv=(b1-(b1-b0)*i/4).toFixed(0)
  h+='<line x1="'+P+'" y1="'+gy+'" x2="'+(W-P)+'" y2="'+gy+'" stroke="#ffffff18"/>'
  h+='<text x="'+(P-4)+'" y="'+(parseFloat(gy)+4)+'" fill="#888" font-size="10" text-anchor="end">'+bv+'</text>'
 }
 h+='<line x1="'+P+'" y1="'+P+'" x2="'+P+'" y2="'+(H-P)+'" stroke="#444"/>'
 h+='<line x1="'+P+'" y1="'+(H-P)+'" x2="'+(W-P)+'" y2="'+(H-P)+'" stroke="#444"/>'
 var bp=data.map(function(d){return sx(d.pos)+','+sy(d.beats)}).join(' ')
 h+='<polyline points="'+bp+'" fill="none" stroke="#f5a623" stroke-width="2"/>'
 data.forEach(function(d){
  h+='<circle cx="'+sx(d.pos)+'" cy="'+sy(d.beats)+'" r="3" fill="#f5a623"/>'
 })
 h+='<text x="'+P+'" y="'+(H-P+14)+'" fill="#888" font-size="10">'+x0.toFixed(3)+'</text>'
 h+='<text x="'+(W-P)+'" y="'+(H-P+14)+'" fill="#888" font-size="10" text-anchor="end">'+x1.toFixed(3)+'</text>'
 h+='<text x="'+(W/2).toFixed(0)+'" y="'+(H-1)+'" fill="#888" font-size="10" text-anchor="middle">Position (in)</text>'
 h+='<text x="'+(W/2).toFixed(0)+'" y="'+(P-6)+'" fill="#f5a623" font-size="10" text-anchor="middle">Beats / window</text>'
 svg.innerHTML=h
}
setInterval(refresh,8000)
refresh()
</script>
</body>
</html>"""

# ── HTTP server ───────────────────────────────────────────────────────────
def handle(conn):
    try:
        conn.settimeout(3)
        req = b''
        while True:
            try:
                chunk = conn.recv(512)
                if not chunk:
                    break
                req += chunk
                if b'\r\n\r\n' in req:
                    break
            except OSError:
                break

        req = req.decode('utf-8', 'ignore')
        parts  = req.split(None, 2)
        method = parts[0] if parts else 'GET'
        path   = parts[1].split('?')[0] if len(parts) > 1 else '/'

        if path in ('/', '/index.html'):
            conn.write(b'HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n')
            for i in range(0, len(HTML), 512):
                conn.write(HTML[i:i+512])

        elif path == '/status':
            elapsed = int(time.time() - cal["windowStart"]) if cal["windowStart"] else 0
            n   = cal["windowBeats"]
            avg = (cal["beatSum"] / n) if n else 0.0
            bph = (n / elapsed * 3600) if elapsed > 0 else 0.0
            body = ujson.dumps({
                "mode":         cal["mode"],
                "lastDirection": cal["lastDirection"],
                "position":     cal["position"],
                "stepSize":     cal["stepSize"],
                "windowSecs":   cal["windowSecs"],
                "windowBeats":  n,
                "avgBeat":      avg,
                "beatsPerHour": bph,
                "elapsed":      elapsed,
                "numSteps":     cal["numSteps"],
                "stepsDone":    cal["stepsDone"],
                "measurements": cal["measurements"],
                "log":          cal["log"],
            })
            conn.write(b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n')
            conn.write(body.encode())

        elif method == 'POST':
            body_parts = req.split('\r\n\r\n', 1)
            payload = {}
            if len(body_parts) > 1 and body_parts[1]:
                try: payload = ujson.loads(body_parts[1])
                except: pass

            if path == '/start':
                if cal["mode"] == "idle":
                    cal["stepsDone"]    = 0
                    cal["measurements"] = []
                    try:
                        with open("calibration.csv", "w") as f:
                            f.write("time,position_in,beats,avg_beat_s\n")
                    except Exception as e:
                        log("CSV init error: {}".format(e))
                    start_window()
            elif path == '/stop':
                cal["mode"]      = "idle"
                cal["stepsLeft"] = 0
                log("Stopped by user.")
            elif path == '/moveUp':
                cal["lastDirection"] = "up"
                queue_move(cal["stepSize"])
            elif path == '/moveDown':
                cal["lastDirection"] = "down"
                queue_move(-cal["stepSize"])
            elif path == '/config':
                if "step" in payload:
                    cal["stepSize"] = float(payload["step"])
                if "windowMins" in payload:
                    cal["windowSecs"] = max(60, int(float(payload["windowMins"]) * 60))
                if "numSteps" in payload:
                    cal["numSteps"] = max(0, int(payload["numSteps"]))
                log("Config: step={:.4f} in  window={} s  numSteps={}".format(
                    cal["stepSize"], cal["windowSecs"], cal["numSteps"]))

            conn.write(b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{}')

    except Exception as e:
        print("HTTP:", e)
    finally:
        conn.close()
        gc.collect()

def web_server():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', 80))
    srv.listen(3)
    log("Web server listening on port 80.")
    while True:
        try:
            conn, _ = srv.accept()
            handle(conn)
        except Exception as e:
            print("Server:", e)

# ── WiFi ──────────────────────────────────────────────────────────────────
def connect_wifi():
    ssid = pwd = hostname = ''
    try:
        with open('wifi.config') as f:
            for line in f:
                k, _, v = line.strip().partition('=')
                if   k == 'ssid':     ssid     = v
                elif k == 'password': pwd      = v
                elif k == 'hostname': hostname = v
    except OSError:
        log("wifi.config not found — cannot connect.")
        return
    import wifi_manager
    wlan = wifi_manager.connectWifi(ssid, pwd, hostname=hostname)
    if wlan and wlan.isconnected():
        log("WiFi: " + wlan.ifconfig()[0])
    else:
        log("WiFi failed.")

# ── Start ─────────────────────────────────────────────────────────────────
connect_wifi()
_thread.start_new_thread(web_server, ())
log("Calibrator ready. Open the web interface to start.")
main_loop()
