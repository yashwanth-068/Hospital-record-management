import os
from functools import wraps
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, g, send_from_directory, abort
import pymysql
import bcrypt
import qrcode
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException

# ---------- Configuration ----------
APP_SECRET = os.getenv('SECRET_KEY', 'a_secure_and_long_secret_key_for_healthcard_pro_2025')
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_USER = os.getenv('DB_USER', 'root')
# WARNING: Storing a literal password is a security risk. Use environment variables or a secrets manager.
DB_PASSWORD = os.getenv('DB_PASSWORD', 'MYsqlpass@123') 
DB_NAME = os.getenv('DB_NAME', 'health_record')

# Path setup
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
QRCODES_DIR = os.path.join(STATIC_DIR, 'qrcodes')
UPLOADS_DIR = os.path.join(STATIC_DIR, 'uploads')

# --- Billing Constants (for India GST) ---
GST_RATE = 0.18 # 18% GST for services (example rate)
CONSULTATION_FEE_DEFAULT = 500.00 # Example consultation fee in INR
TREATMENT_COST_PER_DAY = 1500.00 # Example daily treatment/room charge

# Ensure directories exist
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(QRCODES_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True) 

app = Flask(__name__)
app.secret_key = APP_SECRET
app.config['QR_CODE_FOLDER'] = QRCODES_DIR
app.config['UPLOAD_FOLDER'] = UPLOADS_DIR
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 # 16 Megabytes file limit

# ---------- Database helper ----------
def get_db_conn():
    """Initializes and returns a database connection, storing it in Flask's 'g' object."""
    if 'db_conn' not in g:
        g.db_conn = pymysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME,
            cursorclass=pymysql.cursors.DictCursor, autocommit=False
        )
    return g.db_conn

@app.teardown_appcontext
def close_db_conn(e=None):
    """Closes the database connection at the end of the request."""
    conn = g.pop('db_conn', None)
    if conn is not None:
        conn.close()

# ---------- Security helpers ----------
def hash_pw(plain: str) -> str:
    """Hashes a plain text password using bcrypt."""
    return bcrypt.hashpw(plain.encode('utf-8'), bcrypt.gensalt(12)).decode('utf-8')
# Custom Jinja2 Filter: Newline to Break
def nl2br(value):
    """Converts newlines (\n) in a string to HTML line breaks (<br>)."""
    if value is None:
        return ''
    return value.replace('\n', '<br>')

# Register the custom filter with Jinja2
app.jinja_env.filters['nl2br'] = nl2br
def check_pw(plain: str, hashed: str) -> bool:
    """Checks a plain text password against a stored hash."""
    try:
        # Added check for common password formats to prevent potential issues with non-bcrypt hashes
        if not isinstance(hashed, str) or len(hashed) < 60 or not hashed.startswith('$2b$'): 
            return False 
        return bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        # Catch any potential errors during checkpw (e.g., hash format issues)
        return False
        
# --- UTILITY FUNCTION: Maps timing slot to an approximate hour for alert logic ---
def get_timing_hour(timing_slot):
    """Maps a textual time slot to a 24-hour clock value."""
    slot_map = {
        'Morning': 9,   # 9 AM
        'Noon': 13,     # 1 PM
        'Evening': 18,  # 6 PM
        'Bedtime': 22   # 10 PM
    }
    return slot_map.get(timing_slot, 24) # Default to past midnight if slot is unknown

# ---------- Doctor Availability Helper (MOVED UP FOR GLOBAL CONTEXT) ----------
def get_doctor_availability_status(doctor_id):
    """Fetches the current availability status for a doctor (0 or 1)."""
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT is_accepting_appointments FROM doctor_availability WHERE doctor_id=%s", (doctor_id,))
            result = cur.fetchone()
            # If no record exists, default to not available (False/0)
            return result['is_accepting_appointments'] if result else 0 
    except Exception as e:
        # In case of an error, assume not available to prevent unintended bookings
        app.logger.error(f"Error fetching doctor availability: {e}")
        return 0

# ---------- Auth decorator and context injection ----------
def login_required(role=None):
    """Decorator to enforce login and optional role access."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = session.get('user')
            if not user:
                flash('Session expired. Please verify hospital and log in.', 'warning')
                return redirect(url_for('hospital_login'))
            if role and user.get('role') != role:
                flash(f'Access denied. Only {role.capitalize()} accounts can view this page.', 'danger')
                return redirect(url_for('dashboard')) 
            return fn(*args, **kwargs)
        return wrapper
    return decorator

@app.context_processor
def inject_user_and_now():
    """
    Injects user details, utility functions (calculate_age), and 
    utility helpers (get_doctor_availability_status, get_timing_hour) into all templates.
    """
    def calculate_age(dob_str):
        if not dob_str: return 'N/A'
        try:
            if isinstance(dob_str, date): dob = dob_str
            else: dob = datetime.strptime(str(dob_str), '%Y-%m-%d').date()
            today = date.today()
            age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            return age
        except:
            return 'N/A'

    user = session.get('user', {})
    
    # Inject the helper function itself into the template context
    context = {
        'user': user,
        'now': datetime.now,
        'calculate_age': calculate_age,
        'get_doctor_availability_status': get_doctor_availability_status,
        'get_timing_hour': get_timing_hour 
    }

    # Also inject the simple status for the currently logged-in doctor, if applicable
    if user.get('role') == 'doctor':
        context['doctor_is_available'] = get_doctor_availability_status(user['id'])
        
    return context

# ---------- QR generation and lookup ----------
def generate_qr(patient_id: str) -> str:
    """Generates a QR PNG and returns relative static path"""
    # URL points to the public view of the patient's card
    # IMPORTANT: The public card now includes the full history/discharge summary
    target = url_for('patient_card_public', uid=patient_id, _external=True)
    img = qrcode.make(target)
    filename = f"{patient_id}.png"
    path = os.path.join(app.config['QR_CODE_FOLDER'], filename)
    img.save(path)
    return os.path.join('qrcodes', filename)

# -----------------------------------------------------------------------------
## Core Authentication and Session Flow
# -----------------------------------------------------------------------------

@app.route('/')
def index():
    """Redirects base path to the hospital login page."""
    return redirect(url_for('hospital_login'))

# ------------------- 1. Hospital Verification (Entry Gate) -------------------
@app.route('/hospital_login', methods=['GET', 'POST'])
def hospital_login():
    """Ensures hospital_id is saved to session for subsequent routes."""
    if request.method == 'POST':
        hospital_id = request.form.get('hospital_id', '').strip().upper()
        secret = request.form.get('secret', '').strip()
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                # Assuming 'secret_hash' is used for the master key
                cur.execute("SELECT id, name, secret_hash FROM hospitals WHERE hospital_id=%s", (hospital_id,))
                hospital = cur.fetchone()
                
                if not hospital or not check_pw(secret, hospital['secret_hash']):
                    flash('Invalid Hospital ID or Secret Key provided.', 'danger')
                    return redirect(url_for('hospital_login'))
                
                # Hospital verified successfully: SAVE TO SESSION
                session['verified_hospital_id'] = hospital_id
                session['hospital_name'] = hospital['name']
                
                # Check for existing admin
                cur.execute("SELECT id FROM admins WHERE hospital_id=%s", (hospital_id,))
                if cur.fetchone():
                    flash(f'Hospital "{hospital["name"]}" verified. Proceed to role login.', 'success')
                    return redirect(url_for('login'))
                else:
                    flash(f'Hospital "{hospital["name"]}" verified. Primary Admin setup required.', 'info')
                    # Redirect to admin_register where the session is checked
                    return redirect(url_for('admin_register'))

        except Exception as e:
            flash(f'An unexpected database error occurred: {str(e)}', 'danger')
            return redirect(url_for('hospital_login'))

    return render_template('hospital_login.html')

# ------------------- 2. Role-based Login (Admin/Doctor/Patient) -------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    """Role-based login after hospital verification."""
    hospital_id = session.get('verified_hospital_id')
    hospital_name = session.get('hospital_name', 'Your Hospital')
    
    if not hospital_id:
        flash('Please complete hospital verification first.', 'danger')
        return redirect(url_for('hospital_login'))

    if request.method == 'POST':
        role = request.form.get('role', '').strip()
        identifier = request.form.get('identifier', '').strip()
        password = request.form.get('password', '')
        
        if not all([role, identifier, password]):
            flash('Please fill in all role login fields.', 'danger'); return redirect(url_for('login'))

        conn = get_db_conn()
        table, id_col, identifier_key = '', '', ''

        if role == 'admin':
            table, id_col, identifier_key = 'admins', 'email', 'email'
        elif role == 'doctor':
            table, id_col, identifier_key = 'doctors', 'doctor_id', 'doctor_id'
        elif role == 'patient':
            table, id_col, identifier_key = 'patients', 'patient_id', 'patient_id'
        else:
            flash('Invalid role selected for login.', 'danger'); return redirect(url_for('login'))
        
        try:
            with conn.cursor() as cur:
                # FIX: Corrected SQL Injection vulnerability by using parameterized query
                # The table name cannot be a parameter, so we use string formatting 
                # but ensure the role/table name is strictly controlled by the logic above.
                # The variables 'hospital_id' and 'identifier' *must* be passed as parameters.
                query = f"SELECT * FROM {table} WHERE hospital_id=%s AND {id_col}=%s"
                cur.execute(query, (hospital_id, identifier))
                user = cur.fetchone()
                
                if not user or not check_pw(password, user['password']):
                    flash(f'Invalid credentials for the selected {role.capitalize()} role.', 'danger')
                    return redirect(url_for('login'))

                # Set comprehensive session details
                session['user'] = {
                    'id': user['id'], # This is the DB primary key ID
                    'role': role,
                    'hospital_id': hospital_id,
                    'hospital_name': hospital_name,
                    'name': user.get('name', 'User'),
                    identifier_key: user[id_col]
                }
                flash(f'Welcome, {user["name"]}! Logged in as {role.capitalize()}', 'success')
                return redirect(url_for('dashboard'))
                
        except Exception as e:
            flash(f'A critical login error occurred: {str(e)}', 'danger')
        return redirect(url_for('login'))

    return render_template('role_login.html', hospital_id=hospital_id, hospital_name=hospital_name)

@app.route('/logout')
def logout():
    """Clears the session and redirects to the hospital gate."""
    session.clear()
    flash('You have been securely logged out from HealthCard Pro.', 'info')
    return redirect(url_for('hospital_login'))

# -----------------------------------------------------------------------------
## Registration Routes
# -----------------------------------------------------------------------------

@app.route('/hospital/register', methods=['GET', 'POST'])
def hospital_register():
    """Handles new hospital registration."""
    if request.method == 'POST':
        hospital_id = request.form.get('hospital_id', '').strip().upper()
        name = request.form.get('name', '').strip()
        secret = request.form.get('secret', '').strip()
        # NEW FIELD
        gst_number = request.form.get('gst_number', '').strip()
        if not all([hospital_id, name, secret]):
            flash('All hospital registration fields are mandatory.', 'danger'); return redirect(url_for('hospital_register'))
        
        pw_hash = hash_pw(secret)
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM hospitals WHERE hospital_id=%s", (hospital_id,))
                if cur.fetchone():
                    flash('Error: Hospital ID already exists in the system.', 'danger')
                    return redirect(url_for('hospital_register'))
                
                # Updated SQL to include gst_number
                cur.execute("INSERT INTO hospitals (hospital_id, name, secret_hash, gst_number) VALUES (%s, %s, %s, %s)",
                            (hospital_id, name, pw_hash, gst_number))
                conn.commit()
                flash('Hospital registered successfully! Please proceed to login verification.', 'success')
                return redirect(url_for('hospital_login'))
        except Exception as e:
            conn.rollback(); flash(f'Database insertion error: {str(e)}', 'danger')
        return redirect(url_for('hospital_register'))
    return render_template('hospital_register.html')

@app.route('/admin/register', methods=['GET', 'POST'])
def admin_register():
    """Uses 'verified_hospital_id' to resolve session loss errors for admin setup."""
    hospital_id = session.get('verified_hospital_id')
    hospital_name = session.get('hospital_name', 'Unknown Hospital')
    
    # 1. Validation Check: If the user didn't verify the hospital first
    if not hospital_id:
        flash('Session lost. Please start by verifying your Hospital ID.', 'warning')
        return redirect(url_for('hospital_login'))

    if request.method == 'POST':
        # Retrieve form data
        form_hospital_id = request.form.get('hospital_id', '').strip().upper()
        name = request.form.get('name','').strip()
        email = request.form.get('email','').strip()
        password = request.form.get('password','').strip()
        
        # Security Check: Compare hidden form ID with session ID
        if form_hospital_id != hospital_id:
             flash('Security error: Hospital ID mismatch.', 'danger'); return redirect(url_for('hospital_login'))

        if not all([hospital_id, name, email, password]):
            flash('All admin registration fields are mandatory.', 'danger'); return redirect(url_for('admin_register'))
        
        pw_hashed = hash_pw(password)
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                # Check for existing admin or hospital ID validity (optional, but robust)
                cur.execute("SELECT id FROM hospitals WHERE hospital_id=%s", (hospital_id,))
                if not cur.fetchone():
                    flash('Hospital ID not found. Register the hospital first.', 'danger'); return redirect(url_for('admin_register'))
                
                cur.execute("INSERT INTO admins (hospital_id, name, email, password) VALUES (%s, %s, %s, %s)",
                            (hospital_id, name, email, pw_hashed))
                conn.commit()
                
                flash('Hospital Administrator account created. Please log in through the main gate.', 'success')
                return redirect(url_for('hospital_login'))
                
        except pymysql.err.IntegrityError:
            conn.rollback(); flash(f'Error: Admin with email "{email}" already exists for this hospital.', 'danger')
        except Exception as e:
            conn.rollback(); flash(f'Database error during admin creation: {str(e)}', 'danger')
            
        return redirect(url_for('admin_register'))

    return render_template(
        'admin_register.html', 
        hospital_id=hospital_id,
        hospital_name=hospital_name
    )

@app.route('/doctor_register')
@app.route('/patient_register')
def unauthorized_registration():
    """Blocks direct access to doctor/patient registration."""
    flash("New Doctor/Patient accounts must be created by a Hospital Admin via the Admin Dashboard.", "info")
    return redirect(url_for('login'))

# -----------------------------------------------------------------------------
## Dashboard and Standard User Routes
# -----------------------------------------------------------------------------

@app.route('/dashboard')
@login_required()
def dashboard():
    """Routes logged-in user to their specific dashboard."""
    role = session['user'].get('role')
    if role == 'admin': return redirect(url_for('admin_dashboard'))
    if role == 'doctor': return redirect(url_for('doctor_dashboard'))
    if role == 'patient': return redirect(url_for('patient_dashboard'))
    return abort(403)

# ------------------- Admin Routes (Management) -------------------

@app.route('/admin/dashboard')
@login_required(role='admin')
def admin_dashboard():
    hid = session['user']['hospital_id']
    conn = get_db_conn()
    data = {'doctors': [], 'patients': [], 'total_doctors': 0, 'total_patients': 0}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, doctor_id, specialization FROM doctors WHERE hospital_id=%s ORDER BY name", (hid,))
            data['doctors'] = cur.fetchall()
            data['total_doctors'] = len(data['doctors'])
            
            # Fetch recent patients and also their discharge status
            cur.execute("""
                SELECT p.id, p.name, p.patient_id, p.dob, p.created_at, ds.discharge_date 
                FROM patients p
                LEFT JOIN discharge_summary ds ON p.id = ds.patient_id
                WHERE p.hospital_id=%s ORDER BY p.created_at DESC LIMIT 10
            """, (hid,))
            data['patients'] = cur.fetchall()
            
            cur.execute("SELECT COUNT(id) AS count FROM patients WHERE hospital_id=%s", (hid,))
            data['total_patients'] = cur.fetchone()['count']
            
    except Exception as e:
        flash(f'Error fetching admin dashboard data: {str(e)}', 'danger')
        
    return render_template('admin_dashboard.html', data=data)

@app.route('/admin/add_doctor', methods=['GET','POST'])
@login_required(role='admin')
def admin_add_doctor():
    hid = session['user']['hospital_id']
    if request.method == 'POST':
        doctor_id = request.form.get('doctor_id','').strip().upper()
        name = request.form.get('name','').strip()
        specialization = request.form.get('specialization','').strip()
        password = request.form.get('password','').strip()
        if not all([doctor_id, name, password]):
            flash('Doctor ID, name, and password must be provided.', 'danger'); return redirect(url_for('admin_add_doctor'))
        
        pw_hashed = hash_pw(password)
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO doctors (hospital_id, doctor_id, name, specialization, password) VALUES (%s,%s,%s,%s,%s)",
                            (hid, doctor_id, name, specialization, pw_hashed))
                conn.commit()
                flash(f'New Doctor {name} ({doctor_id}) account created.', 'success')
                return redirect(url_for('admin_dashboard'))
        except pymysql.err.IntegrityError as e:
            conn.rollback()
            if 'Duplicate entry' in str(e) and 'doctor_id' in str(e):
                flash(f'Doctor ID "{doctor_id}" already exists. Please choose a unique ID.', 'danger')
            else:
                flash(f'Database error: {str(e)}', 'danger')
        except Exception as e:
            conn.rollback(); flash(f'Error adding doctor: {str(e)}', 'danger')
        return redirect(url_for('admin_add_doctor'))
    return render_template('admin_add_doctor.html')


@app.route('/admin/add_patient', methods=['GET','POST'])
@login_required(role='admin')
def admin_add_patient():
    hid = session['user']['hospital_id']
    conn = get_db_conn()
    doctors = []
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, doctor_id, name, specialization FROM doctors WHERE hospital_id=%s ORDER BY name", (hid,))
            doctors = cur.fetchall()
    except Exception:
        flash("Error loading doctors list for assignment.", "warning")

    if request.method == 'POST':
        patient_id = request.form.get('patient_id','').strip().upper()
        name = request.form.get('name','').strip()
        dob = request.form.get('dob') or None
        address = request.form.get('address','').strip()
        password = request.form.get('password','').strip()
        assigned_doctor_db_id = request.form.get('assigned_doctor_db_id')

        if not all([patient_id, name, password]):
            flash('Patient ID, full name, and password are required for new card generation.', 'danger')
            return render_template('admin_add_patient.html', doctors=doctors) 
        
        pw_hashed = hash_pw(password)
        
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO patients (hospital_id, patient_id, name, dob, address, password) VALUES (%s,%s,%s,%s,%s,%s)",
                            (hid, patient_id, name, dob, address, pw_hashed))
                patient_db_id = cur.lastrowid
                
                qr_rel = generate_qr(patient_id)
                cur.execute("UPDATE patients SET qr_code_path=%s WHERE id=%s", (qr_rel, patient_db_id))
                
                if assigned_doctor_db_id and patient_db_id:
                    # Note: assigned_doctor_db_id is the doctor's DB primary key ID
                    cur.execute("INSERT INTO doctor_patient (doctor_id, patient_id) VALUES (%s, %s)", 
                                 (assigned_doctor_db_id, patient_db_id))
                    
                conn.commit()
                flash(f'Health Card for {name} ({patient_id}) created. QR code generated and doctor assigned.', 'success')
                return redirect(url_for('admin_dashboard'))
        
        except pymysql.err.IntegrityError as e:
            conn.rollback()
            if 'Duplicate entry' in str(e) and 'patient_id' in str(e):
                flash(f'Error: Patient ID "{patient_id}" already exists. Cannot create duplicate card.', 'danger')
            else:
                flash(f'A database constraint error occurred: {str(e)}', 'danger')
            return render_template('admin_add_patient.html', doctors=doctors)

        except Exception as e:
            conn.rollback()
            flash(f'Critical error during patient card creation: {str(e)}', 'danger')
            return render_template('admin_add_patient.html', doctors=doctors)
            
    return render_template('admin_add_patient.html', doctors=doctors)

# ------------------- NEW ADMIN ROUTE: Discharge and Billing -------------------

# Helper function to calculate billing components
def get_patient_bill_history(conn, patient_db_id):
    """Calculates consultation count, first record date, and estimated costs."""
    bill_data = {
        'first_record_date': None,
        'consultation_count': 0,
        'assigned_doctor_name': 'N/A',
        'estimated_treatment_days': 0,
        'consultation_fee_total': 0.0,
        'treatment_cost_total': 0.0,
        'subtotal': 0.0,
        'gst_rate': GST_RATE,
        'gst_amount': 0.0,
        'total_bill': 0.0,
    }
    
    with conn.cursor() as cur:
        # 1. First Record Date & Consultation Count
        cur.execute("""
            SELECT MIN(created_at) AS first_record, COUNT(id) AS count, doctor_id
            FROM health_records 
            WHERE patient_id = (SELECT patient_id FROM patients WHERE id = %s)
        """, (patient_db_id,))
        record_summary = cur.fetchone()
        
        if not record_summary or not record_summary['count']:
            return bill_data # No records, no billing

        bill_data['first_record_date'] = record_summary['first_record'].date()
        bill_data['consultation_count'] = record_summary['count']
        
        # 2. Assigned Doctor Name (Using the doctor who made the most recent record)
        latest_doctor_id = record_summary['doctor_id'] # This is doctor_id string
        cur.execute("SELECT name FROM doctors WHERE doctor_id=%s", (latest_doctor_id,))
        doctor = cur.fetchone()
        bill_data['assigned_doctor_name'] = doctor['name'] if doctor else 'Unknown Doctor'
        
        # 3. Calculate Days and Costs
        discharge_date = date.today()
        # Calculate days from first recorded visit to discharge (minimum 1 day)
        delta = discharge_date - bill_data['first_record_date']
        bill_data['estimated_treatment_days'] = max(1, delta.days + 1) # Including the first day
        
        # Calculate estimated costs
        bill_data['consultation_fee_total'] = bill_data['consultation_count'] * CONSULTATION_FEE_DEFAULT
        bill_data['treatment_cost_total'] = bill_data['estimated_treatment_days'] * TREATMENT_COST_PER_DAY
        
        bill_data['subtotal'] = bill_data['consultation_fee_total'] + bill_data['treatment_cost_total']
        bill_data['gst_amount'] = bill_data['subtotal'] * bill_data['gst_rate']
        bill_data['total_bill'] = bill_data['subtotal'] + bill_data['gst_amount']
        
    return bill_data

@app.route('/admin/discharge_patient/<int:patient_db_id>', methods=['GET', 'POST'])
@login_required(role='admin')
def admin_discharge_patient(patient_db_id):
    hid = session['user']['hospital_id']
    admin_db_id = session['user']['id']
    conn = get_db_conn()
    
    try:
        with conn.cursor() as cur:
            # 1. Fetch Patient and Hospital Info
            cur.execute("SELECT patient_id, name FROM patients WHERE id=%s AND hospital_id=%s", (patient_db_id, hid))
            patient = cur.fetchone()
            if not patient:
                flash("Patient not found in this hospital.", 'danger'); return redirect(url_for('admin_dashboard'))

            cur.execute("SELECT gst_number FROM hospitals WHERE hospital_id=%s", (hid,))
            hospital_gst = cur.fetchone()['gst_number']
            
            # Check if already discharged
            cur.execute("SELECT 1 FROM discharge_summary WHERE patient_id=%s", (patient_db_id,))
            if cur.fetchone():
                flash(f"Patient {patient['name']} is already discharged. View the summary instead.", 'info')
                return redirect(url_for('admin_dashboard'))

            # 2. GET: Prepare data for the discharge form
            billing_data = get_patient_bill_history(conn, patient_db_id)
            
            if request.method == 'POST':
                # 3. POST: Handle Discharge and Billing Submission
                discharge_date_str = request.form.get('discharge_date')
                treatment_summary = request.form.get('treatment_summary', '').strip()
                discharge_notes = request.form.get('discharge_notes', '').strip()
                
                # Use calculated billing data from GET or recalculate in POST (safer to use values from form/recalculate)
                billing_data_final = get_patient_bill_history(conn, patient_db_id)
                
                # The assigned doctor will be the one who handled the final record
                cur.execute("""
                    SELECT d.id FROM health_records hr 
                    JOIN doctors d ON hr.doctor_id = d.doctor_id
                    WHERE hr.patient_id = %s ORDER BY hr.created_at DESC LIMIT 1
                """, (patient['patient_id'],))
                doctor_db_id_final = cur.fetchone()['id'] if cur.rowcount else None
                
                if not doctor_db_id_final:
                    flash("Cannot discharge: Patient has no consultation records.", 'danger')
                    return redirect(url_for('admin_discharge_patient', patient_db_id=patient_db_id))

                if not all([discharge_date_str, treatment_summary]):
                    flash('Discharge date and Treatment Summary are mandatory.', 'danger')
                    return redirect(url_for('admin_discharge_patient', patient_db_id=patient_db_id))

                # Insert Discharge Summary and Bill
                cur.execute("""
                    INSERT INTO discharge_summary 
                    (patient_id, doctor_id, admission_date, discharge_date, treatment_summary, discharge_notes, 
                    consultation_fee, treatment_cost, subtotal, gst_rate, gst_amount, total_bill)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    patient_db_id, 
                    doctor_db_id_final, 
                    billing_data_final['first_record_date'], 
                    discharge_date_str, 
                    treatment_summary, 
                    discharge_notes,
                    billing_data_final['consultation_fee_total'],
                    billing_data_final['treatment_cost_total'],
                    billing_data_final['subtotal'],
                    billing_data_final['gst_rate'],
                    billing_data_final['gst_amount'],
                    billing_data_final['total_bill']
                ))
                
                discharge_summary_id = cur.lastrowid
                conn.commit()
                flash(f"Patient {patient['name']} discharged and final bill generated.", 'success')
                return redirect(url_for('admin_view_discharge_summary', discharge_id=discharge_summary_id))
                
    except Exception as e:
        conn.rollback()
        flash(f'Critical error during discharge process: {str(e)}', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    return render_template('admin_discharge_patient.html', 
                           patient=patient, 
                           billing_data=billing_data,
                           hospital_gst=hospital_gst,
                           today=date.today().strftime('%Y-%m-%d'))


@app.route('/admin/discharge_summary/<int:discharge_id>')
@login_required(role='admin')
def admin_view_discharge_summary(discharge_id):
    conn = get_db_conn()
    summary = None
    
    try:
        with conn.cursor() as cur:
            # Fetch summary and join relevant tables
            cur.execute("""
                SELECT 
                    ds.*, 
                    p.name AS patient_name, 
                    p.patient_id, 
                    p.dob, 
                    d.name AS doctor_name,
                    h.name AS hospital_name,
                    h.gst_number
                FROM discharge_summary ds
                JOIN patients p ON ds.patient_id = p.id
                JOIN doctors d ON ds.doctor_id = d.id
                JOIN hospitals h ON p.hospital_id = h.hospital_id
                WHERE ds.id = %s
            """, (discharge_id,))
            summary = cur.fetchone()
            
            if not summary:
                flash("Discharge summary not found.", 'danger'); return redirect(url_for('admin_dashboard'))
                
            # Convert dates to strings for template
            summary['admission_date_str'] = summary['admission_date'].strftime('%Y-%m-%d')
            summary['discharge_date_str'] = summary['discharge_date'].strftime('%Y-%m-%d')
            
    except Exception as e:
        flash(f"Error fetching discharge summary: {str(e)}", 'danger')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin_discharge_summary.html', summary=summary)


# ------------------- Doctor Routes (Consultation) -------------------

@app.route('/doctor/dashboard')
@login_required(role='doctor')
def doctor_dashboard():
    user = session['user']; hid = user['hospital_id']; doctor_db_id = user['id']
    conn = get_db_conn()
    patients = []
    total_assigned = 0
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.patient_id, p.name, p.dob, p.address, p.qr_code_path, ds.discharge_date 
                FROM patients p
                JOIN doctor_patient dp ON p.id = dp.patient_id
                LEFT JOIN discharge_summary ds ON p.id = ds.patient_id
                WHERE dp.doctor_id=%s AND p.hospital_id=%s
                ORDER BY p.name ASC
            """, (doctor_db_id, hid))
            patients = cur.fetchall()
            total_assigned = len(patients)
            
    except Exception as e:
        flash(f'Error loading your assigned patients: {str(e)}', 'danger')
        
    return render_template('doctor_dashboard.html', patients=patients, total_assigned=total_assigned)

@app.route('/doctor/patient/<patient_id>', methods=['GET','POST'])
@login_required(role='doctor')
def doctor_patient_view(patient_id):
    user = session['user']; hid = user['hospital_id']; doctor_db_id = user['id'] # Use DB primary key ID
    doctor_id_str = user['doctor_id'] # Use the string identifier for record insertion
    conn = get_db_conn()
    patient = None
    records = []
    
    try:
        with conn.cursor() as cur:
            # 1. Check assignment and fetch Patient details
            cur.execute("""
                SELECT p.* FROM patients p 
                LEFT JOIN doctor_patient dp ON p.id = dp.patient_id 
                WHERE p.patient_id=%s AND p.hospital_id=%s AND (dp.doctor_id=%s OR dp.doctor_id IS NULL)
            """, (patient_id, hid, doctor_db_id))
            patient = cur.fetchone()
            
            if not patient:
                # If patient is not assigned, check if they exist at all in the hospital
                cur.execute("SELECT * FROM patients WHERE patient_id=%s AND hospital_id=%s", (patient_id, hid))
                patient_unassigned = cur.fetchone()
                
                if not patient_unassigned:
                    flash(f'Patient with ID {patient_id} not found in this hospital.', 'danger')
                    return redirect(url_for('doctor_dashboard'))
                
                # Assign the unassigned patient to the doctor's panel now (on first view/post)
                patient = patient_unassigned
                
            # 2. Handle POST request (Adding a health record)
            if request.method == 'POST':
                # Note: The form in doctor_patient_view.html uses 'clinical_notes' and 'legacy_pill_schedule'
                notes = request.form.get('clinical_notes','').strip()
                pill_schedule = request.form.get('legacy_pill_schedule','').strip() 
                
                if not notes:
                    flash('Clinical notes are mandatory for a new consultation record.', 'danger'); 
                else:
                    # Insert the Health Record
                    cur.execute("INSERT INTO health_records (patient_id, doctor_id, notes, pill_schedule) VALUES (%s,%s,%s,%s)",
                                 (patient_id, doctor_id_str, notes, pill_schedule))
                    
                    # Ensure Doctor-Patient Assignment exists
                    # patient['id'] is the patient's DB primary key ID
                    cur.execute("SELECT 1 FROM doctor_patient WHERE doctor_id=%s AND patient_id=%s", 
                                (doctor_db_id, patient['id'])) 
                    
                    if not cur.fetchone():
                        cur.execute("INSERT INTO doctor_patient (doctor_id, patient_id) VALUES (%s, %s)", 
                                    (doctor_db_id, patient['id']))
                        flash(f'Patient {patient["name"]} has been automatically assigned to your panel.', 'info')
                        
                    conn.commit()
                    flash('New Patient Health Record (Clinical Notes) saved successfully.', 'success')
                
                return redirect(url_for('doctor_patient_view', patient_id=patient_id)) 

            # 3. Fetch existing records
            cur.execute("""
                SELECT hr.*, d.name as doctor_name, d.specialization 
                FROM health_records hr 
                LEFT JOIN doctors d ON hr.doctor_id=d.doctor_id 
                WHERE hr.patient_id=%s 
                ORDER BY hr.created_at DESC
            """, (patient_id,))
            records = cur.fetchall()

            # 4. Fetch medication schedules for all records AND Daily Compliance Status
            today = date.today()
            today_str = today.strftime('%Y-%m-%d')

            for record in records:
                # Fetch all medication schedules attached to this record
                cur.execute("""
                    SELECT * FROM medication_schedule 
                    WHERE record_id=%s
                """, (record['id'],))
                schedules = cur.fetchall()
                
                # Check compliance for each schedule
                for schedule in schedules:
                    # Check if the schedule is currently active (or was active today)
                    if schedule['start_date'] <= today and schedule['end_date'] >= today:
                        
                        # Check pill intake log for TODAY
                        cur.execute("""
                            SELECT 1 FROM pill_intake_log 
                            WHERE schedule_id = %s AND DATE(intake_time) = %s
                        """, (schedule['id'], today_str))
                        
                        # Add a new field: taken_today (True if a log exists for today)
                        schedule['taken_today'] = bool(cur.fetchone())
                    else:
                        # Schedule is not active today
                        schedule['taken_today'] = False 
                        
                record['medication_schedules'] = schedules
            
            if patient.get('dob'):
                patient['dob'] = patient['dob'].strftime('%Y-%m-%d')
            
    except Exception as e:
        flash(f'Error accessing patient data: {str(e)}', 'danger'); records = []
        
    return render_template('doctor_patient_view.html', patient=patient, records=records, today_date=date.today())


# --- NEW ROUTE: Doctor adds structured medication schedule for compliance tracking ---
@app.route('/doctor/patient/<patient_id>/add_schedule', methods=['POST'])
@login_required(role='doctor')
def add_medication_schedule(patient_id):
    user = session['user']; hid = user['hospital_id']; doctor_db_id = user['id'] 
    conn = get_db_conn()
    
    try:
        with conn.cursor() as cur:
            # 1. Security Check: Verify patient assignment (or existence)
            cur.execute("""
                SELECT p.id FROM patients p 
                LEFT JOIN doctor_patient dp ON p.id = dp.patient_id 
                WHERE p.patient_id=%s AND p.hospital_id=%s AND (dp.doctor_id=%s OR dp.doctor_id IS NULL)
            """, (patient_id, hid, doctor_db_id))
            patient_check = cur.fetchone()
            if not patient_check:
                flash('Security Error: Patient not found.', 'danger')
                return redirect(url_for('doctor_dashboard'))

            # 2. Get the latest health record ID (must have a record to attach the schedule to)
            cur.execute("""
                SELECT id FROM health_records 
                WHERE patient_id = %s 
                ORDER BY created_at DESC LIMIT 1
            """, (patient_id,))
            record = cur.fetchone()
            if not record:
                flash('Cannot schedule medication: Consultation notes must be saved first to create a record.', 'danger')
                return redirect(url_for('doctor_patient_view', patient_id=patient_id))
            record_id = record['id']
            
            # 3. Get form data (Doctor inputs) - Mapped from HTML form
            pill_name = request.form.get('pill_name') # Mapped to drug_name
            timing_slot = request.form.get('timing_slot') # Correctly named
            dosage = request.form.get('dosage')
            start_date_str = request.form.get('start_date')
            duration_days = request.form.get('duration_days') # Used to calculate end_date

            if not all([record_id, pill_name, timing_slot, dosage, start_date_str, duration_days]):
                flash('Missing medication details for scheduling.', 'danger')
                return redirect(url_for('doctor_patient_view', patient_id=patient_id))
            
            # Calculate End Date from Duration Days
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                duration = int(duration_days)
                # Subtract 1 because the start day is day 1. E.g., duration 7 means day 1 to day 7.
                end_date = start_date + timedelta(days=duration - 1) 
            except ValueError:
                flash('Invalid date or duration format.', 'danger')
                return redirect(url_for('doctor_patient_view', patient_id=patient_id))

            # 4. Insert the scheduled medication into medication_schedule table
            cur.execute("""
                INSERT INTO medication_schedule (record_id, drug_name, timing_slot, dosage, start_date, end_date)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (record_id, pill_name, timing_slot, dosage, start_date, end_date)) # Use pill_name as drug_name
            conn.commit()
            flash(f'Medication {pill_name} scheduled for {timing_slot} tracking.', 'success')
            
    except Exception as e:
        conn.rollback()
        flash(f'Error scheduling medication: {str(e)}', 'danger')
        
    return redirect(url_for('doctor_patient_view', patient_id=patient_id))
# ------------------- Patient Routes (Viewing Records) -------------------
@app.route('/patient/dashboard')
@login_required(role='patient')
def patient_dashboard():
    user = session['user']; hid = user['hospital_id']; patient_id = user['patient_id']
    conn = get_db_conn()
    patient = None
    records = []
    assigned_doctor = None  
    
    # Pill Compliance Tracking
    scheduled_doses = []
    missed_doses_alert = False
    current_hour = datetime.now().hour

    try:
        with conn.cursor() as cur:
            # 1. Fetch Patient details
            cur.execute("SELECT * FROM patients WHERE patient_id=%s AND hospital_id=%s", (patient_id, hid))
            patient = cur.fetchone()
            if not patient: 
                flash('Your Health Card record is incomplete or missing. Please contact administration.', 'danger'); return redirect(url_for('logout'))
            
            # 2. Fetch the assigned doctor's details 
            cur.execute("""
                SELECT d.name, d.specialization 
                FROM doctor_patient dp
                JOIN doctors d ON dp.doctor_id = d.id
                WHERE dp.patient_id = %s
                LIMIT 1
            """, (patient['id'],)) 
            assigned_doctor = cur.fetchone() 
            
            # 3. Fetch Health Records
            cur.execute("""
                SELECT hr.*, d.name as doctor_name, d.specialization 
                FROM health_records hr 
                LEFT JOIN doctors d ON hr.doctor_id=d.doctor_id 
                WHERE hr.patient_id=%s 
                ORDER BY hr.created_at DESC
            """, (patient_id,))
            records = cur.fetchall()
            
            # 4. Fetch Active Medication Schedules AND Compliance Status
            cur.execute("""
                SELECT ms.*, hr.id AS record_id 
                FROM medication_schedule ms
                JOIN health_records hr ON ms.record_id = hr.id
                WHERE hr.patient_id = %s 
                AND ms.start_date <= CURDATE() AND ms.end_date >= CURDATE()
                ORDER BY ms.timing_slot
            """, (patient_id,))
            raw_scheduled_doses = cur.fetchall()

            scheduled_doses_final = []
            
            for dose in raw_scheduled_doses:
                # Check if a dose was logged TODAY
                cur.execute("""
                    SELECT intake_time FROM pill_intake_log 
                    WHERE schedule_id = %s AND DATE(intake_time) = CURDATE()
                    ORDER BY intake_time DESC
                """, (dose['id'],))
                
                intake_logs = cur.fetchall()
                # Store the formatted times for display
                dose['intake_logs'] = [log['intake_time'].strftime('%H:%M') for log in intake_logs]
                # Database uses 'drug_name', but the HTML form uses 'pill_name'. We unify on 'drug_name' for logic.
                dose['drug_name'] = dose['drug_name'] 
                dose['taken_today'] = bool(intake_logs) # Is the dose taken at least once today?

                required_hour = get_timing_hour(dose['timing_slot'])
                
                # Compliance Check (Alert Logic): If required time has passed and not logged
                if required_hour < current_hour and not dose['taken_today']:
                    missed_doses_alert = True
                
                scheduled_doses_final.append(dose)
            
            # 5. Fetch Discharge Status (for patient info/access to bill)
            cur.execute("""
                SELECT id, discharge_date FROM discharge_summary WHERE patient_id=%s
            """, (patient['id'],))
            discharge_summary = cur.fetchone()
            patient['discharge_status'] = bool(discharge_summary)
            if discharge_summary:
                patient['discharge_summary_id'] = discharge_summary['id']

            
            if patient.get('dob'): patient['dob'] = patient['dob'].strftime('%Y-%m-%d')
            patient['qr_url'] = url_for('serve_qrcode', filename=f"{patient_id}.png") if patient.get('qr_code_path') else None

    except Exception as e:
        flash(f'Error loading your patient dashboard data: {str(e)}', 'danger'); records = []; patient = {}
        
    return render_template('patient_dashboard.html', 
                           patient=patient, 
                           records=records, 
                           assigned_doctor=assigned_doctor,
                           scheduled_doses=scheduled_doses_final, 
                           missed_doses_alert=missed_doses_alert)

# --- NEW ROUTE: Patient logs pill intake ---
@app.route('/patient/log_pill/<int:schedule_id>', methods=['POST'])
@login_required(role='patient')
def log_pill_intake(schedule_id):
    patient_db_id = session['user']['id'] # Patient DB ID
    conn = get_db_conn()
    intake_time = datetime.now()
    
    try:
        with conn.cursor() as cur:
            # 1. Security Check: Verify schedule belongs to the logged-in patient
            cur.execute("""
                SELECT ms.drug_name 
                FROM medication_schedule ms
                JOIN health_records hr ON ms.record_id = hr.id
                JOIN patients p ON hr.patient_id = p.patient_id
                WHERE ms.id = %s AND p.id = %s
            """, (schedule_id, patient_db_id))
            
            schedule = cur.fetchone()
            
            if not schedule:
                flash('Access denied: Schedule not found or does not belong to your record.', 'danger')
                return redirect(url_for('patient_dashboard'))

            # 2. Log the intake time
            cur.execute("""
                INSERT INTO pill_intake_log (schedule_id, intake_time)
                VALUES (%s, %s)
            """, (schedule_id, intake_time))
            conn.commit()
            flash(f"Pill intake for {schedule['drug_name']} successfully logged! Thank you for staying compliant.", 'success')
            
    except Exception as e:
        conn.rollback()
        flash(f'Error logging pill intake: {str(e)}', 'danger')
        
    return redirect(url_for('patient_dashboard'))

# -----------------------------------------------------------------------------
## Creative Function Additions
# -----------------------------------------------------------------------------

@app.route('/profile', methods=['GET', 'POST'])
@login_required()
def my_profile():
    """
    Allows any logged-in user (Admin/Doctor/Patient) to view and 
    update their personal details and password.
    """
    user = session['user']
    role = user['role']
    db_id = user['id']
    
    # Determine table and identifier column based on role
    if role == 'admin': table, id_col = 'admins', 'email'
    elif role == 'doctor': table, id_col = 'doctors', 'doctor_id'
    elif role == 'patient': table, id_col = 'patients', 'patient_id'
    else: return abort(403)

    conn = get_db_conn()
    current_data = None
    
    try:
        with conn.cursor() as cur:
            # Fetch current user data
            # FIX: Using parameterized query (f-string for table name is safe as 'table' is strictly controlled by 'role')
            cur.execute(f"SELECT * FROM {table} WHERE id=%s", (db_id,)) 
            current_data = cur.fetchone()
            if not current_data: 
                flash("Your profile data could not be retrieved.", "danger"); return redirect(url_for('dashboard'))

            if request.method == 'POST':
                # Handle Password Change
                old_password = request.form.get('old_password')
                new_password = request.form.get('new_password')
                
                if new_password and old_password:
                    if check_pw(old_password, current_data['password']):
                        pw_hashed = hash_pw(new_password)
                        # FIX: Using parameterized query
                        cur.execute(f"UPDATE {table} SET password=%s WHERE id=%s", (pw_hashed, db_id))
                        conn.commit()
                        flash('Password updated successfully. Please re-login.', 'success')
                        return redirect(url_for('logout'))
                    else:
                        flash('Old password entered is incorrect.', 'danger')

                # Handle Other Profile Updates (e.g., Name, Address, Specialization)
                updates = []
                params = []
                
                # Dynamic updates based on fields available in all tables
                new_name = request.form.get('name', '').strip()
                if new_name and new_name != current_data.get('name'):
                    updates.append("name=%s")
                    params.append(new_name)
                    session['user']['name'] = new_name # Update session

                # Role-specific fields
                if role == 'doctor':
                    new_specialization = request.form.get('specialization', '').strip()
                    if new_specialization != current_data.get('specialization'):
                        updates.append("specialization=%s")
                        params.append(new_specialization)
                elif role == 'patient':
                    new_address = request.form.get('address', '').strip()
                    if new_address != current_data.get('address'):
                        updates.append("address=%s")
                        params.append(new_address)
                        
                if updates:
                    # FIX: Using parameterized query
                    query = f"UPDATE {table} SET {', '.join(updates)} WHERE id=%s"
                    params.append(db_id)
                    cur.execute(query, params)
                    conn.commit()
                    flash(f'{role.capitalize()} profile details updated successfully.', 'success')
                    # Refresh the page to show new details
                    return redirect(url_for('my_profile'))
                
    except Exception as e:
        conn.rollback()
        flash(f'An error occurred during profile update: {str(e)}', 'danger')

    return render_template(f'{role}_profile.html', profile=current_data)


@app.route('/admin/settings', methods=['GET', 'POST'])
@login_required(role='admin')
def admin_hospital_settings():
    """
    Allows the Admin to view/update hospital details (e.g., name, master secret).
    """
    hid = session['user']['hospital_id']
    conn = get_db_conn()
    hospital_data = None

    try:
        with conn.cursor() as cur:
            # Added gst_number to fetch
            cur.execute("SELECT hospital_id, name, gst_number FROM hospitals WHERE hospital_id=%s", (hid,))
            hospital_data = cur.fetchone()
            
            if request.method == 'POST':
                new_name = request.form.get('name', '').strip()
                new_secret = request.form.get('new_master_secret', '').strip()
                new_gst_number = request.form.get('gst_number', '').strip()
                
                updates = []
                params = []

                if new_name and new_name != hospital_data.get('name'):
                    updates.append("name=%s")
                    params.append(new_name)
                    session['hospital_name'] = new_name # Update session immediately

                if new_gst_number != hospital_data.get('gst_number'):
                    updates.append("gst_number=%s")
                    params.append(new_gst_number)

                if new_secret:
                    # Simplified Master Secret update
                    pw_hash = hash_pw(new_secret)
                    updates.append("secret_hash=%s")
                    params.append(pw_hash)

                if updates:
                    query = f"UPDATE hospitals SET {', '.join(updates)} WHERE hospital_id=%s"
                    params.append(hid)
                    cur.execute(query, params)
                    
                    if new_name: flash("Hospital name updated successfully.", 'success')
                    if new_gst_number: flash("Hospital GST Number updated.", 'success')
                    if new_secret: flash("Master Secret Key updated. Notify all key holders.", 'warning')

                conn.commit()
                return redirect(url_for('admin_hospital_settings'))

    except Exception as e:
        conn.rollback()
        flash(f'Error updating hospital settings: {str(e)}', 'danger')

    return render_template('admin_settings.html', hospital=hospital_data)

# -----------------------------------------------------------------------------
## Utility and Public Routes
# -----------------------------------------------------------------------------

@app.route('/search_patient', methods=['GET'])
@login_required()
def search_patient():
    """Handles patient search by ID or Name for Admins and Doctors."""
    user = session['user']; hid = user['hospital_id']
    results = None
    
    # Use request.args for GET parameters
    query = request.args.get('query', '').strip()
    search_by = request.args.get('search_by', 'id')

    if query:
        conn = get_db_conn()
        results = []
        
        # Prepare the query based on search criteria
        if search_by == 'id':
            search_clause = "p.patient_id LIKE %s"
        elif search_by == 'name':
            search_clause = "p.name LIKE %s"
        else:
            flash("Invalid search criteria.", "danger")
            return render_template('search_patient.html', results=None)

        param = f"%{query}%" # LIKE requires the '%' to be part of the parameter value, not the query string.
        
        try:
            with conn.cursor() as cur:
                # FIX: Corrected SQL Injection vulnerability by using parameterized query for the search value
                # search_clause is determined by controlled logic above, so f-string is acceptable.
                sql_query = f"""
                    SELECT p.patient_id, p.name, p.dob, p.created_at, p.id AS db_id, d.name AS assigned_doctor_name, ds.discharge_date
                    FROM patients p
                    LEFT JOIN doctor_patient dp ON p.id = dp.patient_id
                    LEFT JOIN doctors d ON dp.doctor_id = d.id
                    LEFT JOIN discharge_summary ds ON p.id = ds.patient_id # Include discharge status
                    WHERE p.hospital_id = %s AND {search_clause}
                    ORDER BY p.name ASC
                """
                cur.execute(sql_query, (hid, param))
                
                results = cur.fetchall()
                
                # Filter duplicates if a patient is assigned to multiple doctors
                unique_results = {}
                for row in results:
                    pid = row['patient_id']
                    if pid not in unique_results:
                        # Only show the first assigned doctor in the search results
                        unique_results[pid] = row
                
                results = list(unique_results.values())


        except Exception as e:
            flash(f'Database error during patient search: {str(e)}', 'danger')
            results = []
    
    return render_template('search_patient.html', results=results, search_performed=bool(query))

@app.route('/card/<uid>')
def patient_card_public(uid):
    conn = get_db_conn()
    patient = None
    records = []
    discharge_summary = None # NEW: To carry the full history
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM patients WHERE patient_id=%s", (uid,))
            patient = cur.fetchone()
            if not patient: return render_template('not_found.html', error_msg="Health Card ID not recognized."), 404
            
            # Fetch ALL health records for full history/portability view
            cur.execute("""
                SELECT hr.*, d.name as doctor_name, d.specialization 
                FROM health_records hr 
                LEFT JOIN doctors d ON hr.doctor_id=d.doctor_id 
                WHERE hr.patient_id=%s 
                ORDER BY hr.created_at DESC
            """, (uid,))
            records = cur.fetchall()
            
            # Fetch DISCHARGE SUMMARY if one exists at this hospital
            cur.execute("""
                SELECT 
                    ds.*, 
                    d.name AS doctor_name
                FROM discharge_summary ds
                JOIN doctors d ON ds.doctor_id = d.id
                WHERE ds.patient_id = %s
            """, (patient['id'],))
            discharge_summary = cur.fetchone()
            if discharge_summary:
                discharge_summary['admission_date_str'] = discharge_summary['admission_date'].strftime('%Y-%m-%d')
                discharge_summary['discharge_date_str'] = discharge_summary['discharge_date'].strftime('%Y-%m-%d')
            
            if patient.get('dob'): patient['dob'] = patient['dob'].strftime('%Y-%m-%d')
            cur.execute("SELECT name FROM hospitals WHERE hospital_id=%s", (patient['hospital_id'],))
            hospital_name = cur.fetchone()['name'] if cur.rowcount else "Unknown Hospital"

    except Exception:
        return render_template('not_found.html', error_msg="Error fetching public card data."), 500

    # Pass all data for portability: patient, all records, and discharge summary
    return render_template('patient_card.html', 
                           patient=patient, 
                           records=records, 
                           discharge_summary=discharge_summary, # Added
                           hospital_name=hospital_name, 
                           is_public=True)

# ------------------- Static File Serving -------------------

@app.route('/static/qrcodes/<path:filename>')
def serve_qrcode(filename):
    """Serves QR code images."""
    return send_from_directory(app.config['QR_CODE_FOLDER'], filename)

@app.route('/static/uploads/<path:filename>')
def uploaded_file(filename):
    """Serves uploaded documents/images."""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# -----------------------------------------------------------------------------
## NEW APPOINTMENT MANAGEMENT ROUTES
# -----------------------------------------------------------------------------

# Route 1: Doctor/Admin Toggle Availability
@app.route('/doctor/toggle-availability', methods=['POST'])
@login_required(role='doctor')
def toggle_doctor_availability_route():
    doctor_db_id = session['user']['id']
    conn = get_db_conn()
    
    try:
        with conn.cursor() as cur:
            # Check current status
            cur.execute("SELECT is_accepting_appointments FROM doctor_availability WHERE doctor_id=%s", (doctor_db_id,))
            current_status = cur.fetchone()

            if current_status:
                # Toggle existing status
                new_status = not current_status['is_accepting_appointments']
                cur.execute("UPDATE doctor_availability SET is_accepting_appointments=%s WHERE doctor_id=%s", (new_status, doctor_db_id))
            else:
                # Insert initial record (defaulting to True if they just clicked the button)
                new_status = True
                cur.execute("INSERT INTO doctor_availability (doctor_id, is_accepting_appointments) VALUES (%s, %s)", (doctor_db_id, new_status))
            
            conn.commit()
            flash(f"Appointment booking is now {'ON' if new_status else 'OFF'}.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error updating availability status: {str(e)}", "danger")
    
    return redirect(url_for('doctor_dashboard'))


# Route 2: Patient Booking Form (GET/POST)
@app.route('/patient/book-appointment', methods=['GET', 'POST'])
@login_required(role='patient')
def book_appointment():
    patient_db_id = session['user']['id']
    hid = session['user']['hospital_id']
    conn = get_db_conn()
    
    if request.method == 'POST':
        doctor_db_id = request.form.get('doctor_db_id') # Note: using DB ID for foreign key
        date_str = request.form.get('appointment_date')
        time_str = request.form.get('appointment_time')
        reason = request.form.get('reason', '').strip()
        
        if not all([doctor_db_id, date_str, time_str]):
            flash('All date/time/doctor fields are required.', 'danger')
            return redirect(url_for('book_appointment'))
            
        try:
            with conn.cursor() as cur:
                # Input validation: Ensure the doctor is still available and belongs to the hospital
                cur.execute("""
                    SELECT 1 FROM doctors d 
                    JOIN doctor_availability da ON d.id = da.doctor_id
                    WHERE d.id=%s AND d.hospital_id=%s AND da.is_accepting_appointments=1
                """, (doctor_db_id, hid))
                if not cur.fetchone():
                    flash("The selected doctor is no longer available for booking.", "warning")
                    return redirect(url_for('book_appointment'))
                    
                # Create new Appointment record (Status: Pending)
                cur.execute("""
                    INSERT INTO appointments (patient_id, doctor_id, appointment_date, appointment_time, reason, status)
                    VALUES (%s, %s, %s, %s, %s, 'Pending')
                """, (patient_db_id, doctor_db_id, date_str, time_str, reason))
                
                conn.commit()
                flash("Appointment request submitted successfully! Awaiting confirmation from the doctor.", "success")
                return redirect(url_for('patient_dashboard')) 
                
        except Exception as e:
            conn.rollback()
            flash(f"Error submitting appointment: {str(e)}", "danger")
            return redirect(url_for('book_appointment'))

    # GET Request: Fetch available doctors
    available_doctors = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.id, d.name, d.specialization 
                FROM doctors d 
                JOIN doctor_availability da ON d.id = da.doctor_id
                WHERE d.hospital_id=%s AND da.is_accepting_appointments=1
                ORDER BY d.name
            """, (hid,))
            available_doctors = cur.fetchall()
            
    except Exception as e:
        flash(f"Error fetching available doctors: {str(e)}", "danger")

    return render_template('patient_appointment.html', doctors=available_doctors)


# Route 3: Doctor/Admin Appointment Management (Pending List)
@app.route('/doctor/manage-appointments')
@login_required(role='doctor')
def manage_appointments():
    doctor_db_id = session['user']['id']
    conn = get_db_conn()
    pending_appointments = []
    
    try:
        with conn.cursor() as cur:
            # We use the doctor's DB primary key ID (a.doctor_id is the foreign key to doctors.id)
            cur.execute("""
                SELECT a.id, a.appointment_date, a.appointment_time, a.reason, p.name AS patient_name, p.patient_id
                FROM appointments a 
                JOIN patients p ON a.patient_id = p.id
                WHERE a.doctor_id=%s AND a.status='Pending'
                ORDER BY a.appointment_date, a.appointment_time
            """, (doctor_db_id,))
            pending_appointments = cur.fetchall()
    except Exception as e:
        flash(f"Error fetching pending appointments: {str(e)}", "danger")
        
    return render_template('doctor_appointments.html', appointments=pending_appointments)


# Route 4 & 5: Confirm/Reject Actions
@app.route('/doctor/appointment/<int:app_id>/<action>', methods=['POST'])
@login_required(role='doctor')
def update_appointment_status(app_id, action):
    doctor_db_id = session['user']['id']
    conn = get_db_conn()
    
    if action not in ['confirm', 'reject']:
        flash("Invalid appointment action.", "warning")
        return redirect(url_for('manage_appointments'))
        
    status = 'Confirmed' if action == 'confirm' else 'Rejected'
    
    try:
        with conn.cursor() as cur:
            # 1. Fetch appointment details and Security check: Ensure the doctor is only updating their own appointments
            # We fetch patient_id here before making changes.
            cur.execute("SELECT patient_id FROM appointments WHERE id=%s AND doctor_id=%s", (app_id, doctor_db_id))
            appointment = cur.fetchone()
            
            if not appointment:
                flash("You do not have permission to modify this appointment or it doesn't exist.", "danger")
                return redirect(url_for('manage_appointments'))
                
            patient_db_id = appointment['patient_id']
            
            # 2. Update appointment status
            cur.execute("UPDATE appointments SET status=%s WHERE id=%s", (status, app_id))

            # 3. CRITICAL NEW LOGIC: If confirmed, create doctor_patient relationship (Patient Assignment)
            if action == 'confirm':
                # Check if relationship already exists
                cur.execute("SELECT 1 FROM doctor_patient WHERE doctor_id=%s AND patient_id=%s", (doctor_db_id, patient_db_id))
                
                if not cur.fetchone():
                    # Create the relationship
                    cur.execute("INSERT INTO doctor_patient (doctor_id, patient_id) VALUES (%s, %s)", 
                                 (doctor_db_id, patient_db_id))
                    
            conn.commit()
            flash(f"Appointment {app_id} has been {status.lower()}! The patient is now assigned to your panel.", "success")
            
    except pymysql.err.IntegrityError as e:
        conn.rollback()
        if 'Duplicate entry' in str(e):
             # Ignore duplicate error if patient was already assigned, but keep the status change
             conn.commit() 
             flash(f"Appointment {app_id} confirmed. Patient was already on your panel.", "success")
        else:
             flash(f"Error updating status: {str(e)}", "danger")
    except Exception as e:
        conn.rollback()
        flash(f"Error updating status: {str(e)}", "danger")
        
    return redirect(url_for('manage_appointments'))


# ------------------- Error Handlers -------------------

@app.errorhandler(404)
def page_not_found(e):
    return render_template('not_found.html', error_msg="Error 404: The page or resource you are looking for could not be found."), 404

@app.errorhandler(403)
def forbidden(e):
    return render_template('not_found.html', error_msg="Error 403: Access is denied. You do not have permission to view this resource."), 403

# Handles file size limits
@app.errorhandler(HTTPException)
def handle_exception(e):
    if isinstance(e, HTTPException) and e.code == 413:
        flash("File upload failed: File size exceeds the 16MB limit.", "danger")
        return redirect(request.url), 413
    return e


# -----------------------------------------------------------------------------
## End of Application
# -----------------------------------------------------------------------------

if __name__ == '__main__':
    # Ensure directories exist before running
    for d in [STATIC_DIR, QRCODES_DIR, UPLOADS_DIR]:
        os.makedirs(d, exist_ok=True)
        
    # This line tells Flask to listen on ALL available IP addresses 
    # (including your PC's IP on the hotspot network).
    app.run(host='0.0.0.0', port=5000, debug=False)  