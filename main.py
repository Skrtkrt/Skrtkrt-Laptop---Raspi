from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_socketio import SocketIO, emit
import os
import json
import math
import uuid
import sqlite3
import threading
import datetime
import time
from app import hardware
from app.hardware import perform_object_sequence
from app.compass import Compass

# Initialize Flask app
app = Flask(__name__)
app.secret_key = "your-secret-key"  # Required for session
socketio = SocketIO(app)

# Get the current directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Store target location
target_location = None
# Store point coordinates
point_coordinates = {
    'A': None,
    'B': None,
    'C': None,
    'D': None
}
# Store current pattern
current_pattern = None
# Store docking station location
docking_station = None
# Store schedules
schedules = {}

# Dummy credentials (in real app, use proper authentication)
CREDENTIALS = {
    "admin": "password"
}

DB_PATH = os.path.join(BASE_DIR, 'schedules.db')

# Initialize database
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        point TEXT UNIQUE,
        day TEXT,
        time TEXT,
        pattern TEXT,
        radius INTEGER,
        lat REAL,
        lng REAL,
        week INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS docking_station (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        lat REAL,
        lng REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS points (
        point TEXT PRIMARY KEY,
        lat REAL,
        lng REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS target_location (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        lat REAL,
        lng REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        point TEXT,
        lat REAL,
        lng REAL,
        ph REAL,
        ph_voltage REAL,
        do REAL,
        do_voltage REAL,
        turbidity REAL,
        temp REAL,
        water_level REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS error_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        error_type TEXT,
        message TEXT,
        component TEXT,
        severity TEXT,
        water_level REAL,
        gps_lat REAL,
        gps_lng REAL
    )''')
    conn.commit()
    conn.close()

init_db()

# --- Global State for Locking and Automation ---
control_lock = threading.Lock()
current_status = {'state': 'idle'}  # 'idle', 'manual', 'automating'
automation_thread = None
stop_automation_flag = threading.Event()

# Global navigation thread and lock
nav_thread = None
nav_thread_lock = threading.Lock()
nav_status = {'running': False, 'target': None, 'last': None}

# New pattern thread and stop event
pattern_thread = None
pattern_stop_event = threading.Event()

# --- Helper: Set Status ---
def set_status(state):
    current_status['state'] = state

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username')
    password = request.form.get('password')

    if username == "admin" and password == "password":
        session['user'] = username
        return redirect(url_for('main'))
    else:
        return redirect(url_for('index', error='invalid_credentials'))

@app.route('/main')
def main():
    if 'user' not in session:
        return redirect(url_for('index'))
    return render_template('main.html')

@app.route('/maps')
def maps():
    return render_template('maps.html')

@app.route('/schedules')
def schedules_page():
    return render_template('schedules.html')

@app.route('/notifications')
def notifications():
    return render_template('notifications.html')

@app.route('/error-logs')
def error_logs():
    return render_template('error_logs.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route("/read-water")
def read_water():
    try:
        raw, volt = hardware.read_water_level()
        if raw is None or volt is None:
            error_msg = "Failed to read water level: No valid data"
            log_error('WATER_LEVEL_ERROR', error_msg, 'SENSOR', 'ERROR', water_level=None)
            return f"Error reading water level: No valid data", 500
            
        # Log successful reading
        log_error('WATER_LEVEL_INFO', f"Water level reading: raw={raw}, voltage={volt:.2f}V", 
                 'SENSOR', 'INFO', water_level=volt)
        return f"Water level raw: {raw}, Voltage: {volt:.2f} V"
    except Exception as e:
        error_msg = f"Error reading water level: {str(e)}"
        log_error('WATER_LEVEL_ERROR', error_msg, 'SENSOR', 'ERROR', water_level=None)
        return error_msg, 500

@app.route('/move_rover', methods=['POST'])
def move_rover():
    data = request.get_json()
    target_lat = float(data.get('lat'))
    target_lng = float(data.get('lng'))
    pwm = int(data.get('pwm', 40000))  # Default forward PWM
    turn_pwm = 65535  # Default turn PWM

    def haversine(lat1, lon1, lat2, lon2):
        R = 6371000  # meters
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def bearing(lat1, lon1, lat2, lon2):
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dlambda = math.radians(lon2 - lon1)
        y = math.sin(dlambda) * math.cos(phi2)
        x = math.cos(phi1)*math.sin(phi2) - math.sin(phi1)*math.cos(phi2)*math.cos(dlambda)
        brng = math.degrees(math.atan2(y, x))
        return (brng + 360) % 360

    def nav_thread_fn():
        try:
            nav_status['running'] = True
            nav_status['target'] = {'lat': target_lat, 'lng': target_lng}
            pca = hardware.setup_pca9685(channel=0)
            compass = Compass()
            STOP_DIST = 1.5  # meters
            TURN_THRESH = 10  # degrees
            last_gps = None
            for _ in range(120):  # up to 2 minutes
                gps = hardware.get_latest_gps()
                if not gps or not gps['lat'] or not gps['lng']:
                    hardware.stop_motors(pca)
                    nav_status['last'] = 'no_gps'
                    break
                last_gps = gps
                dist = haversine(gps['lat'], gps['lng'], target_lat, target_lng)
                brng = bearing(gps['lat'], gps['lng'], target_lat, target_lng)
                comp = compass.get_compass_data()
                if not comp or 'heading' not in comp:
                    hardware.stop_motors(pca)
                    nav_status['last'] = 'no_compass'
                    break
                heading = comp['heading']
                err = (brng - heading + 360) % 360
                if err > 180:
                    err -= 360
                if dist < STOP_DIST:
                    hardware.stop_motors(pca)
                    nav_status['last'] = 'arrived'
                    break
                if abs(err) > TURN_THRESH:
                    if err > 0:
                        hardware.rotate_right(pca, turn_pwm)
                    else:
                        hardware.rotate_left(pca, turn_pwm)
                else:
                    hardware.move_forward(pca, pwm)
                nav_status['last'] = {'gps': gps, 'compass': comp, 'dist': dist, 'bearing': brng, 'heading': heading, 'err': err}
                time.sleep(0.5)
            hardware.stop_motors(pca)
            nav_status['running'] = False
        except Exception as e:
            nav_status['last'] = f'error: {e}'
            try:
                hardware.stop_motors(pca)
            except:
                pass
            nav_status['running'] = False

    with nav_thread_lock:
        global nav_thread
        if nav_status['running']:
            return jsonify({'status': 'busy', 'message': 'Navigation already in progress'}), 409
        nav_thread = threading.Thread(target=nav_thread_fn, daemon=True)
        nav_thread.start()
    return jsonify({'status': 'started', 'target': {'lat': target_lat, 'lng': target_lng}, 'pwm': pwm})

@app.route('/control_motors', methods=['POST'])
def control_motors():
    data = request.get_json()
    action = data.get('action')

# New route for setting point coordinates
@app.route('/set_point', methods=['POST'])
def set_point():
    data = request.get_json()
    point = data['point']
    lat = data['lat']
    lng = data['lng']
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO points (point, lat, lng)
        VALUES (?, ?, ?)
        ON CONFLICT(point) DO UPDATE SET lat=excluded.lat, lng=excluded.lng
    ''', (point, lat, lng))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'point': {'lat': lat, 'lng': lng}})

# New route for getting point coordinates
@app.route('/get_points')
def get_points():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT point, lat, lng FROM points')
    rows = c.fetchall()
    conn.close()
    # Build dictionary with keys 'A', 'B', 'C', 'D'
    points = {pt: None for pt in ['A', 'B', 'C', 'D']}
    for row in rows:
        points[row[0]] = {'lat': row[1], 'lng': row[2]}
    return jsonify(points)

# New route for setting pattern
@app.route('/set_docking_station', methods=['POST'])
def set_docking_station():
    data = request.get_json()
    lat = data['lat']
    lng = data['lng']
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO docking_station (id, lat, lng)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET lat=excluded.lat, lng=excluded.lng
    ''', (lat, lng))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'docking_station': {'lat': lat, 'lng': lng}})

# New route for getting docking station
@app.route('/get_docking_station')
def get_docking_station():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT lat, lng FROM docking_station WHERE id=1')
    row = c.fetchone()
    conn.close()
    if row and row[0] is not None and row[1] is not None:
        return jsonify({'lat': row[0], 'lng': row[1]})
    else:
        return jsonify({'status': 'no_docking_station'})
    
# New route for getting current pattern
@app.route('/get_pattern')
def get_pattern():
    return jsonify({'pattern': current_pattern})

# Function to calculate distance between two GPS points
def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371  # Earth's radius in kilometers
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2) * math.sin(dlat/2) + \
        math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * \
        math.sin(dlon/2) * math.sin(dlon/2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    distance = R * c
    return distance

@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

@socketio.on('position_update')
def handle_position_update(data):
    try:
        if data.get('type') == 'position_update':
            lat = data.get('lat', 0)
            lng = data.get('lng', 0)

            # Check if within Philippines bounds
            if 4.6431 <= lat <= 21.1205 and 116.9549 <= lng <= 126.5995:
                emit('position', {
                    'type': 'position',
                    'lat': lat,
                    'lng': lng
                })

                # If we have a target, calculate distance and direction
                if target_location:
                    distance = calculate_distance(lat, lng, target_location['lat'], target_location['lng'])
                    emit('target_info', {
                        'type': 'target_info',
                        'distance': distance,
                        'target': target_location
                    })

                    # If we're close to target (within 1 meter)
                    if distance < 0.001:
                        # If we're returning to dock and we've reached it
                        if docking_station and target_location['lat'] == docking_station['lat'] and target_location['lng'] == docking_station['lng']:
                            emit('docking_status', {
                                'type': 'docking_status',
                                'status': 'docked'
                            })
                        # If we're following a pattern
                        elif current_pattern:
                            emit('pattern_progress', {
                                'type': 'pattern_progress',
                                'pattern': current_pattern,
                                'reached_target': True
                            })

                # Check if we're in any point's range
                for point, coords in point_coordinates.items():
                    if coords:
                        distance = calculate_distance(lat, lng, coords['lat'], coords['lng'])
                        if distance <= 0.015:  # Within 15 meters
                            emit('point_range', {
                                'type': 'point_range',
                                'point': point,
                                'in_range': True,
                                'distance': distance
                            })
            else:
                emit('error', {
                    'type': 'error',
                    'message': 'Position outside Philippines bounds'
                })
    except json.JSONDecodeError:
        emit('error', {
            'type': 'error',
            'message': 'Invalid JSON data'
        })
    except Exception as e:
        print(f"WebSocket error: {e}")

@app.route('/save_schedule', methods=['POST'])
def save_schedule():
    data = request.get_json()
    print('Received schedule data:', data)  # Debug print

    required_fields = ['week', 'point', 'day', 'time', 'pattern', 'radius', 'location']
    missing = [field for field in required_fields if field not in data]
    if missing:
        print('Missing fields:', missing)
        return jsonify({'success': False, 'error': f'Missing fields: {missing}'}), 400

    if not isinstance(data['location'], dict) or 'lat' not in data['location'] or 'lng' not in data['location']:
        print('Invalid location:', data.get('location'))
        return jsonify({'success': False, 'error': 'Invalid location'}), 400

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # Upsert: replace if point exists
        c.execute('''INSERT INTO schedules (point, day, time, pattern, radius, lat, lng, week)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(point) DO UPDATE SET
                        day=excluded.day, time=excluded.time, pattern=excluded.pattern,
                        radius=excluded.radius, lat=excluded.lat, lng=excluded.lng, week=excluded.week''',
                  (data['point'], data['day'], data['time'], data['pattern'], data['radius'],
                   data['location']['lat'], data['location']['lng'], data['week']))
        conn.commit()
        conn.close()
        print('Schedule saved successfully')
        return jsonify({'success': True})
    except Exception as e:
        print('Error saving schedule:', e)
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/get_schedules')
def get_schedules():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT point, day, time, pattern, radius, lat, lng, week FROM schedules')
    rows = c.fetchall()
    conn.close()
    schedules = []
    for row in rows:
        schedules.append({
            'point': row[0],
            'day': row[1],
            'time': row[2],
            'pattern': row[3],
            'radius': row[4],
            'location': {'lat': row[5], 'lng': row[6]},
            'week': row[7]
        })
    return jsonify(schedules)

# --- Background Scheduler Thread ---
def scheduler_thread():
    while True:
        now = datetime.datetime.now()
        weekday = now.strftime('%A')
        current_time = now.strftime('%H:%M')
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT point, lat, lng FROM schedules WHERE day=? AND time=?', (weekday, current_time))
        due = c.fetchall()
        conn.close()
        for point, lat, lng in due:
            # Simulate navigation, YOLO, arm, and sampling
            # (Replace with real navigation and YOLO logic as needed)
            print(f"[SCHEDULER] Navigating to {point} at {lat},{lng}")
            # Move arm to sample position
            hardware.set_arm_angle(90)
            time.sleep(2)
            # Read all sensors
            ph = hardware.read_ph()
            do = hardware.read_do()
            try:
                turbidity_voltage = hardware.get_latest_turbidity()
                turbidity = {'voltage': turbidity_voltage}
            except Exception as e:
                print(f"[ERROR] Turbidity sensor: {e}")
                turbidity = {'voltage': None, 'error': str(e)}
            temp = hardware.read_temp()
            water_level = hardware.read_water_level()
            # Log sample
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''INSERT INTO samples (timestamp, point, lat, lng, ph, ph_voltage, do, do_voltage, turbidity, temp, water_level)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
                now.isoformat(), point, lat, lng,
                ph['ph'], ph['voltage'],
                do['do'], do['voltage'],
                turbidity['voltage'], temp, water_level['voltage']
            ))
            conn.commit()
            conn.close()
            print(f"[SCHEDULER] Sample logged for {point}")
        time.sleep(60)  # Check every minute

# Start scheduler in background
threading.Thread(target=scheduler_thread, daemon=True).start()

# --- API Endpoints for Live Data and History ---
@app.route('/api/live_sample')
def api_live_sample():
    try:
        try:
            ph = hardware.read_ph()
        except Exception as e:
            print(f"[ERROR] pH sensor: {e}")
            ph = {'ph': None, 'voltage': None, 'error': str(e)}
        try:
            do = hardware.read_do()
        except Exception as e:
            print(f"[ERROR] DO sensor: {e}")
            do = {'do': None, 'voltage': None, 'error': str(e)}
        try:
            turbidity_voltage = hardware.get_latest_turbidity()
            if turbidity_voltage is None:
                turbidity_voltage = hardware.read_turbidity()
            turbidity = {'voltage': turbidity_voltage}
        except Exception as e:
            print(f"[ERROR] Turbidity sensor: {e}")
            turbidity = {'voltage': None, 'error': str(e)}
        try:
            temp = hardware.read_temp()
        except Exception as e:
            print(f"[ERROR] Temp sensor: {e}")
            temp = None
        try:
            water_level_raw, water_level_voltage = hardware.read_water_level()
        except Exception as e:
            print(f"[ERROR] Water level sensor: {e}")
            water_level_raw, water_level_voltage = None, None

        if water_level_voltage is None:
            water_status = "disconnected"
            detected = None
        else:
            water_status = "connected"
            detected = "Yes" if water_level_voltage > 0.2 else "No"

        return jsonify({
            'ph': ph,
            'do': do,
            'turbidity': turbidity,
            'temp': temp,
            'water_level': {
                'raw': water_level_raw,
                'voltage': water_level_voltage,
                'status': water_status,
                'detected': detected
            }
        })
    except Exception as e:
        print(f"[API ERROR] /api/live_sample: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/sample_history')
def api_sample_history():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT timestamp, point, lat, lng, ph, do, turbidity, temp, water_level FROM samples ORDER BY timestamp DESC LIMIT 100')
    rows = c.fetchall()
    conn.close()
    history = [
        {
            'timestamp': row[0],
            'point': row[1],
            'lat': row[2],
            'lng': row[3],
            'ph': row[4],
            'do': row[5],
            'turbidity': row[6],
            'temp': row[7],
            'water_level': row[8]
        }
        for row in rows
    ]
    return jsonify(history)

@app.route('/api/sample_history/delete/<int:sample_id>', methods=['POST'])
def delete_sample_history(sample_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM samples WHERE id = ?', (sample_id,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/save_breeding_site', methods=['POST'])
def save_breeding_site():
    data = request.get_json()
    # Required fields: ph, do, turbidity, temp, water_level, lat, lng, point
    ph = data.get('ph')
    do = data.get('do')
    turbidity = data.get('turbidity')
    temp = data.get('temp')
    water_level = data.get('water_level')
    lat = data.get('lat')
    lng = data.get('lng')
    point = data.get('point')
    ph_voltage = data.get('ph_voltage')
    do_voltage = data.get('do_voltage')
    timestamp = datetime.datetime.now().isoformat()
    # Only save if all required fields are present and criteria are met
    if (ph is not None and 5.26 <= ph <= 8.41 and
        do is not None and 0.5 <= do <= 2.6 and
        turbidity is not None and 0.29 <= turbidity <= 731.00 and
        temp is not None and 26.80 <= temp <= 33.20 and
        water_level is not None):
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''INSERT INTO samples (timestamp, point, lat, lng, ph, ph_voltage, do, do_voltage, turbidity, temp, water_level) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (timestamp, point, lat, lng, ph, ph_voltage, do, do_voltage, turbidity, temp, water_level))
            conn.commit()
            conn.close()
            return jsonify({'status': 'success'})
        except Exception as e:
            return jsonify({'status': 'error', 'message': str(e)}), 500
    else:
        return jsonify({'status': 'error', 'message': 'Criteria not met or missing data'}), 400

# --- API: GPS Data ---
@app.route('/api/gps')
def api_gps():
    gps = hardware.get_latest_gps()
    # Store in DB if valid
    if gps['lat'] is not None and gps['lng'] is not None:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS gps_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            lat REAL,
            lng REAL,
            raw TEXT,
            date TEXT,
            time TEXT
        )''')
        conn.commit()
        # Use date/time from GPS if available, else try to parse NMEA
        date_str = gps.get('date')
        time_str = gps.get('time')
        if not date_str or not time_str:
            if gps.get('raw'):
                try:
                    parts = gps['raw'].split(',')
                    if gps['raw'].startswith('$GPRMC'):
                        if len(parts) > 9:
                            time_str = f"{parts[1][:2]}:{parts[1][2:4]}:{parts[1][4:6]}"
                            date_str = f"{parts[9][4:6]}/{parts[9][2:4]}/{parts[9][:2]}"
                    elif gps['raw'].startswith('$GPGGA'):
                        if len(parts) > 1:
                            time_str = f"{parts[1][:2]}:{parts[1][2:4]}:{parts[1][4:6]}"
                            now = datetime.datetime.now()
                            date_str = now.strftime("%m/%d/%Y")
                except Exception as e:
                    print(f"Error parsing GPS date/time: {e}")
        c.execute('INSERT INTO gps_log (timestamp, lat, lng, raw, date, time) VALUES (?, ?, ?, ?, ?, ?)',
                  (datetime.datetime.now().isoformat(), gps['lat'], gps['lng'], gps['raw'], date_str, time_str))
        conn.commit()
        conn.close()
        gps['date'] = date_str
        gps['time'] = time_str
    else:
        log_error('GPS_ERROR', 'No valid GPS data received', 'GPS', 'WARNING', 
                 gps_lat=gps.get('lat'), gps_lng=gps.get('lng'))
    return jsonify(gps)

# --- API: Ultrasonic Sensors ---
@app.route('/api/ultrasonic')
def api_ultrasonic():
    return jsonify(hardware.get_ultrasonic())

# --- API: YOLOv8 Object Detection ---
@app.route('/api/yolo')
def api_yolo():
    # This may take a few seconds to run
    return jsonify(hardware.run_yolo_inference())

# --- Mission Progress Status API (Demo) ---
@app.route('/api/mission_status')
def api_mission_status():
    # Demo/mock data: in real use, update with actual mission logic
    # Example: {'A': 'visited', 'B': 'in_progress', 'C': 'pending', 'D': 'pending', 'docking': 'docked'}
    status = {
        'A': 'visited',
        'B': 'in_progress',
        'C': 'pending',
        'D': 'pending',
        'docking': 'docked'  # or 'en_route', 'away'
    }
    return jsonify(status)

# Add error logging function
def log_error(error_type, message, component, severity, water_level=None, gps_lat=None, gps_lng=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''INSERT INTO error_logs 
                    (timestamp, error_type, message, component, severity, water_level, gps_lat, gps_lng)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                 (datetime.datetime.now().isoformat(), error_type, message, component, 
                  severity, water_level, gps_lat, gps_lng))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error logging failed: {str(e)}")

# Add endpoint to get error logs
@app.route('/api/error-logs')
def get_error_logs():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT timestamp, error_type, message, component, severity, water_level, gps_lat, gps_lng FROM error_logs ORDER BY timestamp DESC LIMIT 100')
    rows = c.fetchall()
    conn.close()
    logs = [
        {
            'timestamp': row[0],
            'error_type': row[1],
            'message': row[2],
            'component': row[3],
            'severity': row[4],
            'water_level': row[5],
            'gps_lat': row[6],
            'gps_lng': row[7]
        }
        for row in rows
    ]
    return jsonify(logs)

# Add endpoint to clear error logs
@app.route('/api/error-logs/clear', methods=['POST'])
def clear_error_logs():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM error_logs')
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/test')
def test():
    return render_template('test.html')

# Updated /read-turbidity route to return only the voltage value
@app.route('/read-turbidity')
def read_turbidity():
    try:
        voltage = hardware.get_latest_turbidity()
        if voltage is None:
            return 'No data', 503
        return f"{voltage:.3f}"
    except Exception as e:
        error_msg = f"Error reading turbidity: {str(e)}"
        log_error('TURBIDITY_ERROR', error_msg, 'SENSOR', 'ERROR')
        return error_msg, 500

@app.route('/api/yolo_detection')
def yolo_detection():
    try:
        # Run YOLOv8 inference
        results = hardware.run_yolo_inference()
        
        if 'error' in results:
            return jsonify({'error': results['error']}), 500
            
        # Map YOLOv8 class names to our object labels
        yolo_to_obj = {
            'plant_pot': 'A',
            'bamboo_stump': 'B',
            'tire': 'C',
            'bamboo_planter': 'D'
        }
        
        # Get the highest confidence detection
        detected_object = None
        highest_conf = 0
        
        for obj in results['objects']:
            class_name = obj['class']
            confidence = obj['confidence']
            
            if confidence > highest_conf and class_name in yolo_to_obj:
                highest_conf = confidence
                detected_object = yolo_to_obj[class_name]
        
        return jsonify({
            'object': detected_object,
            'confidence': highest_conf if detected_object else 0
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/manual_control', methods=['POST'])
def manual_control():
    data = request.get_json()
    action = data.get('action')
    pwm = data.get('pwm', 32768)
    try:
        # Only set up PCA9685 for DC motors at 1000Hz (do not reset all or set freq every time)
        pca = hardware.setup_pca9685(channel=0, frequency=1000)  # 1000Hz for DC motors
        # Do NOT reset all or change frequency here!
        if action == 'forward':
            hardware.move_forward(pca, pwm)
        elif action == 'backward':
            hardware.move_backward(pca, pwm)
        elif action == 'left':
            hardware.rotate_left(pca, pwm)
        elif action == 'right':
            hardware.rotate_right(pca, pwm)
        elif action == 'northwest':
            hardware.move_northwest(pca, pwm)
        elif action == 'northeast':
            hardware.move_northeast(pca, pwm)
        elif action == 'southwest':
            hardware.move_southwest(pca, pwm)
        elif action == 'southeast':
            hardware.move_southeast(pca, pwm)
        elif action == 'stop':
            hardware.stop_motors(pca)
        else:
            return jsonify({'status': 'error', 'message': 'Unknown action'}), 400
        return jsonify({'status': 'success', 'action': action})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/control')
def control():
    return render_template('control.html')

# --- Endpoint: Manual Servo Arm Control ---
@app.route('/servo_control', methods=['POST'])
def servo_control():
    if not control_lock.acquire(blocking=False):
        return jsonify({'status': 'busy', 'message': 'System is busy'}), 409
    try:
        data = request.get_json()
        channel = int(data.get('channel'))
        angle = int(data.get('angle'))
        # 1. Reset all channels at 50Hz
        pca = hardware.setup_pca9685(channel=0, frequency=50)
        hardware.reset_all_channels(pca)
        # 2. Move to requested angle
        clamped = hardware.set_servo_angle(pca, channel, angle)
        time.sleep(0.5)
        # 3. Move back to initial position
        init_angle = hardware.servo_config[channel]["init"]
        hardware.smooth_move(pca, channel, clamped, init_angle, hardware.servo_config[channel]["delay"])
        # 4. Reset all channels again
        hardware.reset_all_channels(pca)
        set_status('manual')
        return jsonify({'status': 'success', 'channel': channel, 'angle': clamped})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        control_lock.release()
        set_status('idle')

# --- Endpoint: Reset All Channels ---
@app.route('/reset_channels', methods=['POST'])
def reset_channels():
    if not control_lock.acquire(blocking=False):
        return jsonify({'status': 'busy', 'message': 'System is busy'}), 409
    try:
        pca = hardware.setup_pca9685(channel=0)
        hardware.reset_all_channels(pca)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        control_lock.release()
        set_status('idle')

# --- Endpoint: Status ---
@app.route('/status')
def status():
    return jsonify(current_status)

# --- Automation Logic ---
def automation_sequence():
    set_status('automating')
    try:
        while not stop_automation_flag.is_set():
            # 1. Move forward for 5 seconds (motors)
            pca = hardware.setup_pca9685(channel=0, frequency=1000)
            hardware.move_forward(pca, 32768)
            time.sleep(5)
            hardware.stop_motors(pca)
            # 2. Servo arm action (safe sequence)
            pca = hardware.setup_pca9685(channel=0, frequency=50)
            hardware.reset_all_channels(pca)
            hardware.smooth_move(pca, 9, 70, 20, hardware.servo_config[9]["delay"])
            time.sleep(1)
            hardware.smooth_move(pca, 9, 20, 70, hardware.servo_config[9]["delay"])
            hardware.reset_all_channels(pca)
            time.sleep(1)
            # 3. Repeat or add more logic as needed
    finally:
        pca = hardware.setup_pca9685(channel=0, frequency=1000)
        hardware.stop_motors(pca)
        pca = hardware.setup_pca9685(channel=0, frequency=50)
        hardware.reset_all_channels(pca)
        set_status('idle')
        control_lock.release()

# --- Endpoint: Start/Stop Automation ---
@app.route('/automation', methods=['POST'])
def automation():
    global automation_thread
    data = request.get_json()
    action = data.get('action')
    if action == 'start':
        if not control_lock.acquire(blocking=False):
            return jsonify({'status': 'busy', 'message': 'System is busy'}), 409
        stop_automation_flag.clear()
        automation_thread = threading.Thread(target=automation_sequence, daemon=True)
        automation_thread.start()
        return jsonify({'status': 'started'})
    elif action == 'stop':
        stop_automation_flag.set()
        return jsonify({'status': 'stopping'})
    else:
        return jsonify({'status': 'error', 'message': 'Unknown action'}), 400

@app.route('/move_to_object', methods=['POST'])
def move_to_object():
    data = request.get_json()
    object_name = data.get('object')
    try:
        # Run the arm movement in a background thread
        threading.Thread(target=perform_object_sequence, args=(object_name,), daemon=True).start()
        return jsonify({'status': 'started'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/pump_on', methods=['POST'])
def pump_on_route():
    try:
        hardware.pump_on()
        return jsonify({'status': 'on'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/pump_off', methods=['POST'])
def pump_off_route():
    try:
        hardware.pump_off()
        return jsonify({'status': 'off'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/pattern/circle', methods=['POST'])
def api_pattern_circle():
    global pattern_thread, pattern_stop_event
    data = request.get_json()
    lat = float(data['lat'])
    lng = float(data['lng'])
    radius = float(data.get('radius', 0.2))
    if pattern_thread and pattern_thread.is_alive():
        return jsonify({'status': 'Pattern already running'})
    pattern_stop_event.clear()
    def run_pattern():
        from app import hardware
        hardware.run_circle_pattern(lat, lng, radius, pattern_stop_event)
    pattern_thread = threading.Thread(target=run_pattern, daemon=True)
    pattern_thread.start()
    return jsonify({'status': 'Circle pattern started'})

@app.route('/api/pattern/stop', methods=['POST'])
def api_pattern_stop():
    global pattern_stop_event
    pattern_stop_event.set()
    return jsonify({'status': 'Pattern stopped'})

if __name__ == '__main__':
    try:
        import eventlet
        import eventlet.wsgi
        socketio.run(app, host="127.0.0.1", port=5000, debug=True)
    except Exception as e:
        print(f"Error running server: {e}")
    finally:
        # No need to cleanup here as hardware is initialized per request
        pass
