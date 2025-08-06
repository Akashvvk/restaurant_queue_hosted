import sqlite3
import os
import datetime
import requests
from flask import Flask, request, render_template, redirect, url_for, Response, flash
from dotenv import load_dotenv
from flask_cors import CORS
import json

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
# Add a secret key for flash messages
app.secret_key = os.getenv("FLASK_SECRET_KEY", "a_default_secret_key_for_development")
CORS(app)

# --- Admin Dashboard & App Configuration ---
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "supersecret")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
API_URL = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
FLOW_ID = os.getenv("FLOW_ID", "YOUR_FLOW_ID")

# --- Global State Variables ---
user_states = {}
AUTO_ALLOCATOR_ENABLED = True

# ======================================================================
# --- DATABASE FUNCTIONS ---
# ======================================================================

def get_db_connection():
    """Creates a database connection with a row factory for dict-like access."""
    conn = sqlite3.connect("users.db", detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the database and ensures tables have the necessary columns."""
    conn = get_db_connection()
    cursor = conn.cursor()
    # Users table is now the live WAITING QUEUE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT,
            name TEXT,
            people_count INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_number TEXT NOT NULL UNIQUE,
            capacity INTEGER NOT NULL,
            status TEXT DEFAULT 'free',
            occupied_by_user_id INTEGER,
            occupied_timestamp DATETIME,
            customer_name TEXT,
            people_count INTEGER,
            customer_phone_number TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
    
    # NEW: Customer History table for analytics
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customer_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone_number TEXT,
            people_count INTEGER,
            arrival_timestamp DATETIME NOT NULL,
            seated_timestamp DATETIME,
            departed_timestamp DATETIME,
            table_number TEXT
        )""")
        
    # Add unique constraint to users phone_number if it doesn't exist. NULLs are not considered unique.
    try:
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_phone_number ON users(phone_number) WHERE phone_number IS NOT NULL;")
    except sqlite3.OperationalError:
        pass # Index might already exist in older setups

    conn.commit()

    initial_tables_config = []
    # 9 two-seater tables
    for i in range(1, 10):
        initial_tables_config.append((f"T{i}", 2))
    # 29 four-seater tables
    for i in range(10, 39):
        initial_tables_config.append((f"T{i}", 4))
    # 8 six-seater tables
    for i in range(39, 47):
        initial_tables_config.append((f"T{i}", 6))
    for table_num, capacity in initial_tables_config:
        cursor.execute("INSERT OR IGNORE INTO tables (table_number, capacity, status) VALUES (?, ?, 'free')", (table_num, capacity))
    
    conn.commit()
    conn.close()
    print("Database 'users.db' initialized with history table.")


init_db()

# ======================================================================
# --- WHATSAPP & CORE LOGIC FUNCTIONS ---
# ======================================================================

def send_message(to, text, msg_type="text", interactive_payload=None):
    """Sends a WhatsApp message."""
    # Do not send messages if 'to' is None (for walk-in customers)
    if not to:
        print(f"INFO: No phone number provided. Skipping WhatsApp message for walk-in.")
        return

    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = { "messaging_product": "whatsapp", "to": to }

    if msg_type == "text":
        payload["type"] = "text"
        payload["text"] = {"body": text}
    elif msg_type == "interactive" and interactive_payload:
        payload["type"] = "interactive"
        payload["interactive"] = interactive_payload
    else:
        print(f"Unsupported message type: {msg_type}")
        return

    try:
        response = requests.post(API_URL, json=payload, headers=headers)
        response.raise_for_status()
        print(f"OUTGOING to {to}: {text if msg_type == 'text' else 'Interactive Message'}")
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"FAILED sending message to {to}: {e}")
        return {"error": str(e)}

def add_customer_to_queue(name, people_count, phone_number=None):
    """Adds a customer to the waiting queue, with a proper check for duplicates."""
    conn = get_db_connection()
    try:
        # Only check for a duplicate if a phone number is actually provided.
        if phone_number:
            existing_user = conn.execute("SELECT id FROM users WHERE phone_number = ?", (phone_number,)).fetchone()
            if existing_user:
                print(f"Error: Phone number {phone_number} already in queue.")
                return False  # A user with this phone number already exists.

        # If no phone number was provided, or if the provided number is not a duplicate, add the new customer.
        conn.execute(
            "INSERT INTO users (phone_number, name, people_count, timestamp) VALUES (?, ?, ?, ?)",
            (phone_number, name, people_count, datetime.datetime.now())
        )
        conn.commit()
        return True
    except sqlite3.Error as e:
        print(f"Database error in add_customer_to_queue: {e}")
        return False
    finally:
        conn.close()
        
def update_customer_in_queue(phone_number, new_name, new_people_count):
    """Updates an existing user's details in the queue."""
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE users SET name = ?, people_count = ?, timestamp = ? WHERE phone_number = ?",
            (new_name, new_people_count, datetime.datetime.now(), phone_number)
        )
        conn.commit()
        print(f"Updated queue for {phone_number} with name: {new_name}, party size: {new_people_count}")
    except sqlite3.Error as e:
        print(f"Database error in update_customer_in_queue: {e}")
    finally:
        conn.close()

def update_table_status_to_free(table_number):
    """Marks a table as free and updates customer history."""
    conn = get_db_connection()
    try:
        # Find the customer who was at this table
        table_info = conn.execute("SELECT customer_phone_number FROM tables WHERE table_number = ?", (table_number,)).fetchone()
        
        # Mark their departure time in the history
        if table_info and table_info['customer_phone_number']:
            conn.execute("""
                UPDATE customer_history SET departed_timestamp = ? 
                WHERE phone_number = ? AND departed_timestamp IS NULL
            """, (datetime.datetime.now(), table_info['customer_phone_number']))
        else: # Handle walk-ins not having a phone number
             conn.execute("""
                UPDATE customer_history SET departed_timestamp = ? 
                WHERE table_number = ? AND departed_timestamp IS NULL
            """, (datetime.datetime.now(), table_number))


        # Now, clear the table
        cursor = conn.execute("""
            UPDATE tables SET 
                status = 'free', occupied_by_user_id = NULL, occupied_timestamp = NULL,
                customer_name = NULL, people_count = NULL, customer_phone_number = NULL
            WHERE table_number = ?
        """, (table_number,))
        
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        print(f"Database error in update_table_status_to_free: {e}")
        return False
    finally:
        conn.close()

def seat_customer(customer_id, table_id):
    """Assigns a customer to a table, moves them to history, and removes from queue."""
    conn = get_db_connection()
    try:
        # Get customer and table details
        customer = conn.execute("SELECT * FROM users WHERE id = ?", (customer_id,)).fetchone()
        table = conn.execute("SELECT * FROM tables WHERE id = ?", (table_id,)).fetchone()
        
        if not customer or not table:
            print("Error: Could not find customer or table to seat.")
            return

        # 1. Move customer to history table
        conn.execute("""
            INSERT INTO customer_history (name, phone_number, people_count, arrival_timestamp, seated_timestamp, table_number)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            customer['name'], customer['phone_number'], customer['people_count'],
            customer['timestamp'], datetime.datetime.now(), table['table_number']
        ))

        # 2. Update the table with the customer's info
        conn.execute("""
            UPDATE tables SET 
                status = 'occupied', occupied_by_user_id = ?, occupied_timestamp = ?,
                customer_name = ?, people_count = ?, customer_phone_number = ?
            WHERE id = ?
        """, (
            customer_id, datetime.datetime.now(), customer['name'],
            customer['people_count'], customer['phone_number'], table_id
        ))
        
        # 3. Remove customer from the waiting queue
        conn.execute("DELETE FROM users WHERE id = ?", (customer_id,))
        
        conn.commit()
        
        # 4. Notify the customer
        send_message(customer['phone_number'], f"Great news, {customer['name']}! Your table ({table['table_number']}) is ready. Please proceed to the host.")
        print(f"SEATED: {customer['name']} ({customer['people_count']}p) at Table {table['table_number']}")

    except sqlite3.Error as e:
        print(f"Database error in seat_customer: {e}")
    finally:
        conn.close()

def attempt_seating_allocation():
    """The core auto-allocator logic."""
    if not AUTO_ALLOCATOR_ENABLED:
        print("Auto-allocator is disabled. Skipping allocation run.")
        return

    print("Running auto-allocator...")
    conn = get_db_connection()
    waiting_customers = conn.execute("SELECT * FROM users ORDER BY timestamp ASC").fetchall()
    free_tables = conn.execute("SELECT * FROM tables WHERE status = 'free' ORDER BY capacity ASC").fetchall()
    conn.close()
    
    if not waiting_customers or not free_tables:
        print("No customers waiting or no free tables. Allocation ends.")
        return

    seated_customer_ids = set()
    occupied_table_ids = set()

    # This logic is recursive to try and fill as many tables as possible in one run
    for customer in waiting_customers:
        if customer['id'] in seated_customer_ids:
            continue

        best_fit_table = None
        min_waste = float('inf')

        for table in free_tables:
            if table['id'] in occupied_table_ids:
                continue
            
            if table['capacity'] >= customer['people_count']:
                waste = table['capacity'] - customer['people_count']
                if waste < min_waste:
                    min_waste = waste
                    best_fit_table = table
        
        if best_fit_table:
            # Use the main seat_customer function which now handles all DB operations
            seat_customer(customer['id'], best_fit_table['id'])
            
            # Recursive call to see if more customers can be seated with remaining tables
            attempt_seating_allocation()
            return # Exit after the first seating to restart the process with updated lists

# ======================================================================
# --- ADMIN DASHBOARD HELPER FUNCTIONS ---
# ======================================================================

def get_waiting_customers():
    """Fetches waiting customers and ensures timestamp is a datetime object."""
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM users ORDER BY timestamp ASC").fetchall()
    conn.close()

    customers = []
    for row in rows:
        # Convert the database row to a mutable dictionary
        row_dict = dict(row)

        # Manually convert the timestamp string to a datetime object
        timestamp_str = row_dict.get('timestamp')
        if timestamp_str and isinstance(timestamp_str, str):
            try:
                # Handle timestamps with or without microseconds
                if '.' in timestamp_str:
                    row_dict['timestamp'] = datetime.datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S.%f')
                else:
                    row_dict['timestamp'] = datetime.datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                # If parsing fails, set to a default or None to prevent crashes
                row_dict['timestamp'] = None
        
        customers.append(row_dict)

    return customers

def get_free_tables():
    conn = get_db_connection()
    tables = conn.execute("SELECT * FROM tables WHERE status = 'free' ORDER BY capacity ASC").fetchall()
    conn.close()
    return tables

def get_all_tables():
    conn = get_db_connection()
    tables = conn.execute("SELECT * FROM tables ORDER BY CAST(SUBSTR(table_number, 2) AS INTEGER)").fetchall()
    conn.close()
    return tables

def get_occupied_tables():
    """Fetches occupied tables and ensures timestamp is a datetime object."""
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM tables WHERE status = 'occupied' ORDER BY occupied_timestamp DESC").fetchall()
    conn.close()
    
    occupied = []
    for row in rows:
        # Convert the database row to a mutable dictionary
        row_dict = dict(row)
        
        # Manually convert the timestamp string to a datetime object
        timestamp_str = row_dict.get('occupied_timestamp')
        if timestamp_str and isinstance(timestamp_str, str):
            try:
                # Handle timestamps with or without microseconds
                if '.' in timestamp_str:
                    row_dict['occupied_timestamp'] = datetime.datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S.%f')
                else:
                    row_dict['occupied_timestamp'] = datetime.datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                # If parsing fails, set to None to avoid breaking the template
                row_dict['occupied_timestamp'] = None
        
        occupied.append(row_dict)
        
    return occupied
def get_customer_history():
    """Fetches all customer records for the analytics page and ensures timestamps are datetime objects."""
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM customer_history ORDER BY arrival_timestamp DESC").fetchall()
    conn.close()
    
    history_data = []
    for row in rows:
        row_dict = dict(row)
        
        # List of all timestamp fields in the history table to check and convert
        timestamp_fields = ['arrival_timestamp', 'seated_timestamp', 'departed_timestamp']
        
        for field in timestamp_fields:
            timestamp_str = row_dict.get(field)
            # Check if the field exists, is not None, and is a string
            if timestamp_str and isinstance(timestamp_str, str):
                try:
                    if '.' in timestamp_str:
                        row_dict[field] = datetime.datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S.%f')
                    else:
                        row_dict[field] = datetime.datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                except (ValueError, TypeError):
                    row_dict[field] = None # Set to None if parsing fails
        
        history_data.append(row_dict)
        
    return history_data
def remove_user_from_queue(customer_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM users WHERE id = ?", (customer_id,))
    conn.commit()
    conn.close()

def check_auth(username, password):
    return username == ADMIN_USER and password == ADMIN_PASSWORD

def authenticate():
    return Response('Login Required', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})

# ======================================================================
# --- FLASK ROUTES ---
# ======================================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not (data and data.get("object") == "whatsapp_business_account"):
        return "Not a WhatsApp event", 400

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if not (change.get("field") == "messages" and value.get("messages")):
                continue

            msg = value["messages"][0]
            sender = msg["from"]
            msg_type = msg.get("type")
            
            print(f"INCOMING from {sender}: {json.dumps(msg, indent=2)}")

            # Use .setdefault() to safely get or create the user state
            user_state_data = user_states.setdefault(sender, {"state": "initial"})
            current_state = user_state_data.get("state")

            # --- 1. Handle TEXT messages based on state ---
            if msg_type == "text":
                content = msg["text"]["body"].lower().strip()

                if current_state == "initial":
                    # Keywords to start the customer queue flow
                    if content in ["dljflkasjdflkfj","book now"]:
                        conn = get_db_connection()
                        existing_user = conn.execute("SELECT * FROM users WHERE phone_number = ?", (sender,)).fetchone()
                        conn.close()
                        
                        if existing_user:
                            # User is already waiting, ask to update
                            update_payload = { "type": "button", "body": {"text": f"Hi {existing_user['name']}! You are already in our waiting queue for {existing_user['people_count']} people. Would you like to update your details?"}, "action": { "buttons": [{"type": "reply", "reply": {"id": "start_update_flow", "title": "Yes, Update"}}, {"type": "reply", "reply": {"id": "cancel_update", "title": "No, Keep it"}}]}}
                            send_message(sender, text="", msg_type="interactive", interactive_payload=update_payload)
                        else:
                            # New user, start the main customer flow
                            flow_payload = { "type": "flow", "header": {"type": "text", "text": "Welcome!"}, "body": {"text": "Please provide your details to join the waiting list."}, "footer": {"text": "Tap the button below to start."}, "action": {"name": "flow", "parameters": {"flow_message_version": "3", "flow_token": f"token_{sender}", "flow_id": FLOW_ID, "flow_cta": "Join Queue", "flow_action": "navigate", "flow_action_payload": {"screen": "customer_details_screen"}}}}
                            send_message(sender, text="", msg_type="interactive", interactive_payload=flow_payload)
                            user_state_data["state"] = "awaiting_flow"
                    
                    # Keyword to start the WAITER flow
                    elif content == "waiter":
                        send_message(sender, "Please enter the waiter password.")
                        user_state_data["state"] = "awaiting_waiter_password"


                elif current_state == "awaiting_waiter_password":
                    if content == "waiter123": # The waiter password
                        send_message(sender, "Authenticated. Enter the table number to mark as free (e.g., T5 or 5).")
                        user_state_data["state"] = "awaiting_table_number"
                    else:
                        send_message(sender, "Incorrect password. Returning to customer mode.")
                        user_states.pop(sender, None) # Reset state completely

                elif current_state == "awaiting_table_number":
                    table_to_free = content.upper()
                    if not table_to_free.startswith('T') and table_to_free.isdigit():
                        table_to_free = f"T{table_to_free}"

                    if update_table_status_to_free(table_to_free):
                        send_message(sender, f"Success! Table {table_to_free} is now free. Checking queue...")
                        attempt_seating_allocation()
                    else:
                        send_message(sender, f"Could not free table {table_to_free}. Please check the table number and try again.")
                    
                    user_states.pop(sender, None) # Reset state after the action

            # --- 2. Handle INTERACTIVE messages (Flows and Buttons) ---
            elif msg_type == "interactive":
                interactive_data = msg.get("interactive", {})
                
                if interactive_data.get("type") == "nfm_reply":
                    response_json_str = interactive_data["nfm_reply"].get("response_json")
                    response_data = json.loads(response_json_str)
                    name = response_data.get("name", "").title()
                    people_count = int(response_data.get("people_count", 0))

                    if name and 1 <= people_count <= 10:
                        if current_state == "awaiting_update_flow":
                            update_customer_in_queue(sender, name, people_count)
                            send_message(sender, f"Thanks! We've updated your booking for {people_count} people under the name {name}.")
                        else:
                            add_customer_to_queue(name, people_count, sender)
                            send_message(sender, f"Thank you, {name}! You're in the queue for a party of {people_count}. We'll message you when your table is ready.")
                        
                        user_states.pop(sender, None) # Reset state
                        attempt_seating_allocation()
                    else:
                        send_message(sender, "Invalid details. Name is required and party size must be between 1-10.")

                elif interactive_data.get("type") == "button_reply":
                    button_id = interactive_data["button_reply"]["id"]
                    if button_id == "start_update_flow":
                        flow_payload = { "type": "flow", "header": {"type": "text", "text": "Update Details"}, "body": {"text": "Please provide your new details."}, "footer": {"text": "Tap the button below to start."}, "action": {"name": "flow", "parameters": {"flow_message_version": "3", "flow_token": f"token_update_{sender}", "flow_id": FLOW_ID, "flow_cta": "Update Form", "flow_action": "navigate", "flow_action_payload": {"screen": "customer_details_screen"}}}}
                        send_message(sender, text="", msg_type="interactive", interactive_payload=flow_payload)
                        user_state_data["state"] = "awaiting_update_flow"
                    elif button_id == "cancel_update":
                        send_message(sender, "No problem! We'll keep your existing details.")
                        user_states.pop(sender, None)

    return "EVENT_RECEIVED", 200

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Verification failed", 403

# --- ADMIN DASHBOARD ROUTES ---
@app.route('/admin')
def admin_dashboard():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    return render_template('admin.html',
        customers=get_waiting_customers(),
        all_tables=get_all_tables(),
        free_tables=get_free_tables(),
        occupied_tables=get_occupied_tables(),
        auto_allocator_status='ON' if AUTO_ALLOCATOR_ENABLED else 'OFF'
    )

@app.route('/admin/toggle_auto_allocator', methods=['POST'])
def toggle_auto_allocator():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password): return authenticate()
    
    global AUTO_ALLOCATOR_ENABLED
    AUTO_ALLOCATOR_ENABLED = not AUTO_ALLOCATOR_ENABLED
    if AUTO_ALLOCATOR_ENABLED:
        attempt_seating_allocation()
        
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/free_table', methods=['POST'])
def free_table():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password): return authenticate()
    
    if update_table_status_to_free(request.form.get('table_number')):
        attempt_seating_allocation()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/remove_customer', methods=['POST'])
def remove_customer():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password): return authenticate()
    
    remove_user_from_queue(request.form.get('customer_id'))
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/run_auto_seat', methods=['POST'])
def run_auto_seat():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password): return authenticate()
    
    attempt_seating_allocation()
    return redirect(url_for('admin_dashboard'))
    
@app.route('/admin/seat_manually', methods=['POST'])
def seat_manually():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password): return authenticate()
    
    customer_id = request.form.get('customer_id')
    table_id = request.form.get('table_id')

    if customer_id and table_id:
        seat_customer(int(customer_id), int(table_id))
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/add_customer', methods=['POST'])
def add_customer():
    """Handles manual customer additions from the dashboard, with phone number now mandatory."""
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password): return authenticate()

    name = request.form.get('name')
    people_count = request.form.get('people_count')
    phone_number = request.form.get('phone_number')

    # Updated validation: ensure all three fields are filled.
    if not all([name, people_count, phone_number]):
        flash("Name, number of people, and phone number are all required.", "error")
        return redirect(url_for('admin_dashboard'))
    
    if add_customer_to_queue(name.title(), int(people_count), phone_number):
        flash(f"Added {name.title()} ({phone_number}) to the waiting queue.", "success")
        attempt_seating_allocation() # Try to seat them immediately if possible
    else:
        flash(f"The phone number {phone_number} is already in the waiting queue.", "error")

    return redirect(url_for('admin_dashboard'))

@app.route('/admin/history')
def admin_history():
    """NEW route to display the customer history/analytics page."""
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password): return authenticate()

    return render_template('history.html', history_data=get_customer_history())


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)