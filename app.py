import sqlite3
import os
import datetime
import requests
from flask import Flask, request, render_template, redirect, url_for, Response
from dotenv import load_dotenv
from flask_cors import CORS
import json

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
CORS(app)

# --- Admin Dashboard & App Configuration ---
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "supersecret")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
API_URL = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
FLOW_ID = os.getenv("FLOW_ID", "YOUR_FLOW_ID") # Add your Flow ID to your .env file

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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL UNIQUE,
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
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (occupied_by_user_id) REFERENCES users(id) ON DELETE SET NULL
        )""")
    
    # Add columns to store seated customer info directly on the table.
    try:
        cursor.execute("ALTER TABLE tables ADD COLUMN customer_name TEXT;")
    except sqlite3.OperationalError:
        pass # Column already exists
    try:
        cursor.execute("ALTER TABLE tables ADD COLUMN people_count INTEGER;")
    except sqlite3.OperationalError:
        pass # Column already exists
    try: # ADDED: Column for customer phone number
        cursor.execute("ALTER TABLE tables ADD COLUMN customer_phone_number TEXT;")
    except sqlite3.OperationalError:
        pass # Column already exists

    conn.commit()

    initial_tables_config = [
        ("T1", 2), ("T2", 2), ("T3", 2), ("T4", 2),
        ("T5", 4), ("T6", 4), ("T7", 4), ("T8", 4),
        ("T9", 6), ("T10", 6)
    ]
    for table_num, capacity in initial_tables_config:
        cursor.execute("INSERT OR IGNORE INTO tables (table_number, capacity, status) VALUES (?, ?, 'free')", (table_num, capacity))
    
    conn.commit()
    conn.close()
    print("Database 'users.db' initialized.")

init_db() 

# ======================================================================
# --- WHATSAPP & CORE LOGIC FUNCTIONS ---
# ======================================================================

def send_message(to, text, msg_type="text", interactive_payload=None):
    """Sends a WhatsApp message."""
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to
    }

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

def save_user_data_to_db(phone_number, name, people_count):
    """Saves or updates a user in the waiting queue."""
    conn = get_db_connection()
    try:
        existing_user = conn.execute("SELECT id FROM users WHERE phone_number = ?", (phone_number,)).fetchone()
        if existing_user:
            conn.execute("UPDATE users SET name = ?, people_count = ?, timestamp = ? WHERE id = ?", 
                         (name, people_count, datetime.datetime.now(), existing_user['id']))
            user_id = existing_user['id']
        else:
            cursor = conn.execute("INSERT INTO users (phone_number, name, people_count, timestamp) VALUES (?, ?, ?, ?)",
                                  (phone_number, name, people_count, datetime.datetime.now()))
            user_id = cursor.lastrowid
        conn.commit()
        return user_id
    except sqlite3.Error as e:
        print(f"Database error in save_user_data_to_db: {e}")
        return None
    finally:
        conn.close()

def update_table_status_to_free(table_number):
    """Marks a table as free and clears customer data."""
    conn = get_db_connection()
    try:
        cursor = conn.execute("""
            UPDATE tables SET 
                status = 'free', 
                occupied_by_user_id = NULL, 
                occupied_timestamp = NULL,
                customer_name = NULL,
                people_count = NULL,
                customer_phone_number = NULL
            WHERE table_number = ?
        """, (table_number,))
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        print(f"Database error in update_table_status_to_free: {e}")
        return False
    finally:
        conn.close()

def seat_customer(customer_id, table_id, table_number, customer_phone_number, customer_name, people_count):
    """Assigns a customer to a table, notifies them, and removes them from the queue."""
    conn = get_db_connection()
    try:
        conn.execute("""
            UPDATE tables SET 
                status = 'occupied', 
                occupied_by_user_id = ?, 
                occupied_timestamp = ?,
                customer_name = ?,
                people_count = ?,
                customer_phone_number = ?
            WHERE id = ?
        """, (customer_id, datetime.datetime.now(), customer_name, people_count, customer_phone_number, table_id))
        
        conn.execute("DELETE FROM users WHERE id = ?", (customer_id,))
        conn.commit()
        
        send_message(customer_phone_number, f"Great news, {customer_name}! Your table ({table_number}) is ready. Please proceed to the host.")
        print(f"SEATED: {customer_name} ({people_count}p) at Table {table_number}")
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
    waiting_customers = get_waiting_customers()
    free_tables = get_free_tables()
    
    if not waiting_customers or not free_tables:
        print("No customers waiting or no free tables. Allocation ends.")
        return

    seated_customer_ids = set()
    occupied_table_ids = set()

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
            seat_customer(
                customer['id'],
                best_fit_table['id'],
                best_fit_table['table_number'],
                customer['phone_number'],
                customer['name'],
                customer['people_count']
            )
            seated_customer_ids.add(customer['id'])
            occupied_table_ids.add(best_fit_table['id'])
            
            attempt_seating_allocation()
            return

# ======================================================================
# --- ADMIN DASHBOARD HELPER FUNCTIONS ---
# ======================================================================

def get_waiting_customers():
    conn = get_db_connection()
    customers = conn.execute("SELECT id, phone_number, name, people_count, timestamp FROM users ORDER BY timestamp ASC").fetchall()
    conn.close()
    return customers

def get_free_tables():
    conn = get_db_connection()
    tables = conn.execute("SELECT id, table_number, capacity FROM tables WHERE status = 'free' ORDER BY capacity ASC").fetchall()
    conn.close()
    return tables

def get_all_tables():
    conn = get_db_connection()
    tables = conn.execute("SELECT id, table_number, capacity, status FROM tables ORDER BY CAST(SUBSTR(table_number, 2) AS INTEGER)").fetchall()
    conn.close()
    return tables

def get_occupied_tables():
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT table_number, occupied_timestamp, customer_name, people_count, customer_phone_number
        FROM tables 
        WHERE status = 'occupied' 
        ORDER BY occupied_timestamp DESC
    """).fetchall()
    conn.close()

    occupied = []
    for row in rows:
        row_dict = dict(row)
        if row_dict['occupied_timestamp'] and isinstance(row_dict['occupied_timestamp'], str):
            try:
                if '.' in row_dict['occupied_timestamp']:
                    row_dict['occupied_timestamp'] = datetime.datetime.strptime(row_dict['occupied_timestamp'], '%Y-%m-%d %H:%M:%S.%f')
                else:
                    row_dict['occupied_timestamp'] = datetime.datetime.strptime(row_dict['occupied_timestamp'], '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                row_dict['occupied_timestamp'] = None
        occupied.append(row_dict)
        
    return occupied

def remove_user_from_queue(customer_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM users WHERE id = ?", (customer_id,))
    conn.commit()
    conn.close()

def get_user_details(user_id):
    conn = get_db_connection()
    user = conn.execute("SELECT phone_number, name, people_count FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return user

def get_table_details(table_id):
    conn = get_db_connection()
    table = conn.execute("SELECT table_number FROM tables WHERE id = ?", (table_id,)).fetchone()
    conn.close()
    return table

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
    if data and data.get("object") == "whatsapp_business_account":
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if change.get("field") == "messages" and value.get("messages"):
                    msg = value["messages"][0]
                    sender = msg["from"]
                    msg_type = msg.get("type")
                    
                    print(f"INCOMING from {sender}: {json.dumps(msg, indent=2)}")

                    user_state = user_states.setdefault(sender, {"state": "initial"})

                    if msg_type == "text":
                        content = msg["text"]["body"].lower().strip()
                        if user_state["state"] == "initial":
                            if content == "hi" or content == "dljflkasjdflkfj":
                                flow_payload = {
                                    "type": "flow",
                                    "header": {
                                        "type": "text",
                                        "text": "Welcome!"
                                    },
                                    "body": {
                                        "text": "Please provide your details to join the waiting list."
                                    },
                                    "footer": {
                                        "text": "Tap the button below to start."
                                    },
                                    "action": {
                                        "name": "flow",
                                        "parameters": {
                                            "flow_message_version": "3",
                                            "flow_token": "your_flow_token", # Can be any unique string
                                            "flow_id": FLOW_ID,
                                            "flow_cta": "Open Form",
                                            "flow_action": "navigate",
                                            "flow_action_payload": {
                                                "screen": "customer_details_screen"
                                            }
                                        }
                                    }
                                }
                                send_message(sender, text="", msg_type="interactive", interactive_payload=flow_payload)
                                user_state["state"] = "awaiting_flow"

                            elif content == "waiter":
                                send_message(sender, "Please enter the waiter password.")
                                user_state["state"] = "awaiting_waiter_password"
                            else:
                                send_message(sender, "Please say 'hi' to join the waiting list, or 'waiter' for staff functions.")

                        elif user_state["state"] == "awaiting_waiter_password":
                            if content == "waiter123":
                                send_message(sender, "Authenticated. Enter the table number to mark as free (e.g., T5 or 5).")
                                user_state["state"] = "awaiting_table_number"
                            else:
                                send_message(sender, "Incorrect password. Returning to customer mode.")
                                user_states.pop(sender, None)

                        elif user_state["state"] == "awaiting_table_number":
                            table_to_free = content
                            if table_to_free.isdigit():
                                table_to_free = f"T{table_to_free}"
                            else:
                                table_to_free = table_to_free.upper()

                            if update_table_status_to_free(table_to_free):
                                send_message(sender, f"Success! Table {table_to_free} is now free. Checking queue...")
                                attempt_seating_allocation()
                            else:
                                send_message(sender, f"Could not free table {table_to_free}. Please check the table number and try again.")
                            
                            user_states.pop(sender, None)

                    elif msg_type == "interactive" and msg.get("interactive", {}).get("type") == "nfm_reply":
                        flow_response = msg["interactive"]["nfm_reply"]
                        
                        # In some versions, the response is in 'response_json', in others it is 'body'
                        response_json_str = flow_response.get("response_json") or flow_response.get("body")
                        if not response_json_str:
                             print("Could not find response JSON in the flow reply.")
                             return "EVENT_RECEIVED", 200

                        response_json = json.loads(response_json_str)
                        
                        name = response_json.get("name")
                        people_count_str = response_json.get("people_count")

                        try:
                            people_count = int(people_count_str)
                            if name and 1 <= people_count <= 10:
                                if save_user_data_to_db(sender, name.title(), people_count):
                                    send_message(sender, f"Thank you, {name.title()}! You're in the queue for a party of {people_count}. We'll message you when your table is ready.")
                                    user_states.pop(sender, None)
                                    attempt_seating_allocation()
                                else:
                                    send_message(sender, "There was an error saving your details. Please try again.")
                            else:
                                send_message(sender, "Sorry, we can only accommodate parties of 1 to 10, and a name is required.")
                        except (ValueError, TypeError):
                            send_message(sender, "Invalid number of people. Please try again.")
                    
                    elif msg_type != "text":
                        send_message(sender, "Sorry, I can only process text messages or form submissions.")

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
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    global AUTO_ALLOCATOR_ENABLED
    AUTO_ALLOCATOR_ENABLED = not AUTO_ALLOCATOR_ENABLED
    print(f"Auto-allocator status toggled to: {'ON' if AUTO_ALLOCATOR_ENABLED else 'OFF'}")

    if AUTO_ALLOCATOR_ENABLED:
        attempt_seating_allocation()
        
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/free_table', methods=['POST'])
def free_table():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    if update_table_status_to_free(request.form.get('table_number')):
        attempt_seating_allocation()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/remove_customer', methods=['POST'])
def remove_customer():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    remove_user_from_queue(request.form.get('customer_id'))
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/run_auto_seat', methods=['POST'])
def run_auto_seat():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    attempt_seating_allocation()
    return redirect(url_for('admin_dashboard'))
    
@app.route('/admin/seat_manually', methods=['POST'])
def seat_manually():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    customer_id = request.form.get('customer_id')
    table_id = request.form.get('table_id')

    if customer_id and table_id:
        customer = get_user_details(customer_id)
        table = get_table_details(table_id)
        if customer and table:
            seat_customer(
                int(customer_id), 
                int(table_id), 
                table['table_number'], 
                customer['phone_number'], 
                customer['name'],
                customer['people_count']
            )
    return redirect(url_for('admin_dashboard'))

if __name__ == "__main__":
    # This block is now only for local development purposes if you run 'python app.py'
    # On Render, Gunicorn will start the app and init_db() will already have been called.
    app.run(host='0.0.0.0', port=5000, debug=True)