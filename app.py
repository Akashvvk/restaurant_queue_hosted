import os
from flask import Flask, request, render_template, redirect, url_for, Response
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy # Import SQLAlchemy
import datetime
import requests
import json

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# --- APP CONFIGURATION ---
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "supersecret")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
API_URL = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
FLOW_ID = os.getenv("FLOW_ID", "YOUR_FLOW_ID") 

# --- Flask-SQLAlchemy Configuration ---
# Render automatically sets DATABASE_URL for your Postgres service.
# We use .get() with a default for local development (if you still want SQLite locally)
# or you can directly put your Postgres URL from Render here for testing locally too.
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL") 
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False # Suppress SQLAlchemy warning

db = SQLAlchemy(app) # Initialize SQLAlchemy

# --- Global State Variables ---
user_states = {}
AUTO_ALLOCATOR_ENABLED = True


# ======================================================================
# --- DATABASE MODELS (using SQLAlchemy ORM) ---
# ======================================================================

# Define your User model
class User(db.Model):
    __tablename__ = 'users' # Explicitly name table
    id = db.Column(db.Integer, primary_key=True)
    phone_number = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(100))
    people_count = db.Column(db.Integer)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.now)

    def __repr__(self):
        return f'<User {self.name} ({self.phone_number})>'

# Define your Table model
class Table(db.Model):
    __tablename__ = 'tables' # Explicitly name table
    id = db.Column(db.Integer, primary_key=True)
    table_number = db.Column(db.String(10), unique=True, nullable=False)
    capacity = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(10), default='free') # 'free' or 'occupied'

    # Foreign key to User, but handled slightly differently now as we store customer data on Table directly
    occupied_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL')) 
    occupied_timestamp = db.Column(db.DateTime)
    customer_name = db.Column(db.String(100))
    people_count = db.Column(db.Integer)
    customer_phone_number = db.Column(db.String(20))

    # Relationship (optional, but good for linking)
    occupied_by = db.relationship('User', backref='occupied_table', uselist=False)

    def __repr__(self):
        return f'<Table {self.table_number} (Cap: {self.capacity}, Status: {self.status})>'

# ======================================================================
# --- INITIALIZE DATABASE (for SQLAlchemy) ---
# Call this outside the if __name__ == "__main__": block
# so it runs when Gunicorn (Render's web server) starts the app.
# ======================================================================
@app.before_request
def initialize_database():
    # This will create tables if they don't exist.
    # It's good to run this on app startup.
    # We put it in a @app.before_first_request or similar for production
    # or simply outside the if __name__ block
    # For simplicity in this example, we'll run it directly.
    # A more robust solution might use Flask-Migrate for schema changes.
    with app.app_context():
        db.create_all() # Creates tables based on models

        # Add initial tables if they don't exist
        initial_tables_config = [
            ("T1", 2), ("T2", 2), ("T3", 2), ("T4", 2),
            ("T5", 4), ("T6", 4), ("T7", 4), ("T8", 4),
            ("T9", 6), ("T10", 6)
        ]
        for table_num, capacity in initial_tables_config:
            if not Table.query.filter_by(table_number=table_num).first():
                db.session.add(Table(table_number=table_num, capacity=capacity, status='free'))
        db.session.commit()
    print("PostgreSQL database initialized/checked via SQLAlchemy.")

initialize_database() # Call it directly when the app loads

# ======================================================================
# --- WHATSAPP & CORE LOGIC FUNCTIONS (UPDATED FOR SQLALCHEMY) ---
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
    with app.app_context(): # Ensure we are in an application context for DB operations
        user = User.query.filter_by(phone_number=phone_number).first()
        if user:
            user.name = name
            user.people_count = people_count
            user.timestamp = datetime.datetime.now()
        else:
            user = User(phone_number=phone_number, name=name, people_count=people_count)
            db.session.add(user)
        db.session.commit()
        return user.id

def update_table_status_to_free(table_number):
    """Marks a table as free and clears customer data."""
    with app.app_context():
        table = Table.query.filter_by(table_number=table_number).first()
        if table:
            table.status = 'free'
            table.occupied_by_user_id = None
            table.occupied_timestamp = None
            table.customer_name = None
            table.people_count = None
            table.customer_phone_number = None
            db.session.commit()
            return True
        return False

def seat_customer(customer_id, table_id, table_number, customer_phone_number, customer_name, people_count):
    """Assigns a customer to a table, notifies them, and removes them from the queue."""
    with app.app_context():
        table = Table.query.get(table_id)
        customer = User.query.get(customer_id)

        if table and customer:
            table.status = 'occupied'
            table.occupied_by_user_id = customer.id
            table.occupied_timestamp = datetime.datetime.now()
            table.customer_name = customer.name
            table.people_count = customer.people_count
            table.customer_phone_number = customer.phone_number
            db.session.delete(customer) # Remove from waiting queue
            db.session.commit()

            send_message(customer_phone_number, f"Great news, {customer_name}! Your table ({table_number}) is ready. Please proceed to the host.")
            print(f"SEATED: {customer_name} ({people_count}p) at Table {table_number}")
        else:
            print(f"Error seating customer {customer_id} at table {table_id}: Customer or Table not found.")

def attempt_seating_allocation():
    """The core auto-allocator logic."""
    global AUTO_ALLOCATOR_ENABLED # Make sure we can access the global flag
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
        if customer.id in seated_customer_ids: # Use .id for SQLAlchemy objects
            continue

        best_fit_table = None
        min_waste = float('inf')

        for table in free_tables:
            if table.id in occupied_table_ids: # Use .id for SQLAlchemy objects
                continue

            if table.capacity >= customer.people_count: # Use .capacity and .people_count
                waste = table.capacity - customer.people_count
                if waste < min_waste:
                    min_waste = waste
                    best_fit_table = table

    if best_fit_table:
        seat_customer(
            customer.id, # Pass SQLAlchemy object attributes
            best_fit_table.id,
            best_fit_table.table_number,
            customer.phone_number,
            customer.name,
            customer.people_count
        )
        seated_customer_ids.add(customer.id)
        occupied_table_ids.add(best_fit_table.id)

        # Recursively call to seat more customers if possible
        # Be careful with recursion depth on large queues. 
        # For simplicity, we keep it, but for very large queues,
        # consider iterating or using a more robust queueing system.
        attempt_seating_allocation()
        return

# ======================================================================
# --- ADMIN DASHBOARD HELPER FUNCTIONS (UPDATED FOR SQLALCHEMY) ---
# ======================================================================

def get_waiting_customers():
    with app.app_context():
        # SQLAlchemy returns model objects, not dicts. Access attributes directly.
        customers = User.query.order_by(User.timestamp.asc()).all()
        return customers

def get_free_tables():
    with app.app_context():
        tables = Table.query.filter_by(status='free').order_by(Table.capacity.asc()).all()
        return tables

def get_all_tables():
    with app.app_context():
        # Convert table_number (e.g., 'T1', 'T10') to integer for proper sorting
        # This requires some database-specific casting or custom sorting logic.
        # For simplicity, we'll sort alphabetically by table_number string.
        # If you need numerical sort, consider storing table_number as INTEGER or using raw SQL.
        tables = Table.query.order_by(Table.table_number).all() 
        return tables

def get_occupied_tables():
    with app.app_context():
        occupied = Table.query.filter_by(status='occupied').order_by(Table.occupied_timestamp.desc()).all()
        return occupied

def remove_user_from_queue(customer_id):
    with app.app_context():
        customer = User.query.get(customer_id)
        if customer:
            db.session.delete(customer)
            db.session.commit()

def get_user_details(user_id):
    with app.app_context():
        user = User.query.get(user_id)
        return user

def get_table_details(table_id):
    with app.app_context():
        table = Table.query.get(table_id)
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

    # Data for rendering template (now using SQLAlchemy functions)
    customers = get_waiting_customers()
    all_tables = get_all_tables()
    free_tables = get_free_tables()
    occupied_tables = get_occupied_tables()

    # Ensure occupied_timestamp is a datetime object for formatting in Jinja
    for table in occupied_tables:
        if isinstance(table.occupied_timestamp, str):
            try:
                # Try parsing with microseconds first, then without
                if '.' in table.occupied_timestamp:
                    table.occupied_timestamp = datetime.datetime.strptime(table.occupied_timestamp, '%Y-%m-%d %H:%M:%S.%f')
                else:
                    table.occupied_timestamp = datetime.datetime.strptime(table.occupied_timestamp, '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                table.occupied_timestamp = None

    return render_template('admin.html',
        customers=customers,
        all_tables=all_tables,
        free_tables=free_tables,
        occupied_tables=occupied_tables,
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
                customer.id, # Pass SQLAlchemy object attributes
                table.id,
                table.table_number,
                customer.phone_number,
                customer.name,
                customer.people_count
            )
    return redirect(url_for('admin_dashboard'))

if __name__ == "__main__":
    # For local development:
    # If you want to use SQLite locally, you can modify the SQLALCHEMY_DATABASE_URI
    # for local execution. But for consistency, it's often better to try connecting
    # to a local Postgres instance if you're developing with Postgres.
    # Ensure your DATABASE_URL environment variable is set for local testing too,
    # or use a default SQLite path only for __main__

    # Example for local SQLite if DATABASE_URL is not set:
    # if not os.getenv("DATABASE_URL"):
    #     app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///local_users.db'
    #     with app.app_context():
    #         db.create_all() # Create local SQLite tables
    #         # Add initial tables for local SQLite too
    #         for table_num, capacity in initial_tables_config:
    #             if not Table.query.filter_by(table_number=table_num).first():
    #                 db.session.add(Table(table_number=table_num, capacity=capacity, status='free'))
    #         db.session.commit()

    app.run(host='0.0.0.0', port=5000, debug=True)