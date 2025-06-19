import streamlit as st
import os
import json
from datetime import datetime
import smtplib
import imaplib
import email
from email.message import EmailMessage
from dotenv import load_dotenv
import ollama
import re
from streamlit_autorefresh import st_autorefresh

# --- Load environment variables --- #
load_dotenv()
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
ESCALATION_EMAIL = os.getenv("ESCALATION_EMAIL")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")  # Use a standard model, not a 'thinking' variant

ollama_client = ollama.Client(host=OLLAMA_HOST)

def ollama_generate(prompt, model=None, max_tokens=250):
    if model is None:
        model = OLLAMA_MODEL
    response = ollama_client.generate(model=model, prompt=prompt, options={"num_predict": max_tokens})
    return response['response'].strip() if 'response' in response else "[No response from Ollama]"

# --- Load invoice data --- #
def load_invoices(file_path="invoices.json"):
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


# --- Check overdue invoices --- #
def check_overdue(invoices):
    today = datetime.today().date()
    return [
        inv for inv in invoices
        if inv["status"] == "unpaid"
           and datetime.strptime(inv["due_date"], "%Y-%m-%d").date() < today
    ]


# --- Send email --- #
def send_email(recipient, subject, body):
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        raise ValueError("EMAIL_ADDRESS and EMAIL_PASSWORD must be set in the environment.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = recipient
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        smtp.send_message(msg)

def ai_should_escalate(message_text):
    prompt = (
        f"Does the following client message require escalation to a human operator?\n\n"
        f"\"{message_text}\"\n\n"
        f"Reply with YES if a human should handle it, or NO if the AI can reply."
    )
    response = ollama_generate(prompt, max_tokens=5).lower()
    return "yes" in response


# --- Check inbox for replies --- #
def check_inbox(debug=False):
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        raise ValueError("EMAIL_ADDRESS and EMAIL_PASSWORD must be set in the environment.")
    with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
        imap.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        imap.select("inbox")

        status, messages = imap.search(None, '(UNSEEN)')
        email_ids = messages[0].split()

        if debug:
            st.write(f"📨 Found {len(email_ids)} unseen emails.")

        replies = []

        for num in email_ids:
            status, msg_data = imap.fetch(num, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    subject = msg["subject"]
                    from_ = msg["from"]

                    payload = None

                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain" and "attachment" not in str(
                                    part.get("Content-Disposition", "")):
                                payload_bytes = part.get_payload(decode=True)
                                if payload_bytes and isinstance(payload_bytes, bytes):
                                    payload = payload_bytes.decode(errors="ignore")
                                    break
                    else:
                        payload_bytes = msg.get_payload(decode=True)
                        if payload_bytes and isinstance(payload_bytes, bytes):
                            payload = payload_bytes.decode(errors="ignore")

                    if payload and payload.strip():
                        replies.append((from_, subject, payload.strip()))

        return replies


# --- AI-generated reply --- #
def generate_reply(message_text):
    prompt = (
        f"As a professional credit controller at Interprais, respond to this client message: \n\n"
        f"\"{message_text}\"\n\n"
        f"Craft a thoughtful, human-like email reply based on the client's message. "
        f"If they mention they will pay soon, thank them and ask for a specific payment date. "
        f"If they say they've already paid, politely apologize for any discrepancy and assure them that you'll double-check the records and confirm shortly. "
        f"If they express concerns or reasons for delay, show understanding and encourage them to suggest a realistic payment date. "
        f"Use a warm, respectful, and professional tone. Keep the reply concise and clear."
    )
    response = ollama_generate(prompt, max_tokens=200)
    return response



# --- AI-generated invoice reminder --- #
def generate_initial_reminder(client_name, amount, due_date):
    prompt = (
        f"Write a concise, professional, and polite email reminder to {client_name} about their overdue invoice of ₦{amount} due on {due_date}. "
        f"Do not include any reasoning, explanations, or extra commentary—just the email content. "
        f"Do not add a signature or company contact info. "
        f"Start directly with the greeting and end with a warm closing, but do not include your name or company details. "
        f"Reply in this format:\n\n"
        f"Subject: [A short subject line]\n\n"
        f"Dear {client_name},\n\n"
        f"[Email body]\n\n"
        f"Thank you."
    )
    response = ollama_generate(prompt, max_tokens=250)
    return response


# --- Utility function: extract_email_address --- #
def extract_email_address(full_address):
    match = re.search(r'<(.+?)>', full_address)
    if match:
        return match.group(1)
    return full_address.strip()


# --- UI --- #
st.set_page_config(page_title="AI Credit Controller", page_icon="📧", layout="wide")
st.title("📧 AI Credit Controller")

# --- Two-column layout --- #
col1, col2 = st.columns([1, 1])

# --- Control Panel & Status (Left Column) --- #
with col1:
    st.header("⚙️ Control Panel & Status")
    # Interval controls
    st.subheader("Automation Settings")
    if "interval" not in st.session_state:
        st.session_state["interval"] = 60  # Default to 1 minute
    interval = st.number_input(
        "Interval (seconds) for background tasks",
        min_value=30,
        max_value=3600,
        value=st.session_state["interval"],
        step=30,
        help="How often to check for overdue invoices and new emails."
    )
    st.session_state["interval"] = interval
    # Display summary stats
    invoices = load_invoices()
    overdue_count = len(check_overdue(invoices))
    st.metric("Overdue Invoices", overdue_count)
    st.metric("Total Invoices", len(invoices))
    # You can add more metrics as needed

# --- Activity Log (Right Column) --- #
with col2:
    st.header("📋 Activity Log")
    log_placeholder = st.empty()
    if "log_history" not in st.session_state:
        st.session_state["log_history"] = []

# --- Polling/Automation Logic --- #
# Use st_autorefresh to refresh the app at the selected interval
st_autorefresh(interval=interval * 1000, key="autorefresh")

# Only run automation logic once per refresh
if "last_run" not in st.session_state or st.session_state["last_run"] != st.session_state["interval"]:
    st.session_state["last_run"] = st.session_state["interval"]
    # --- Overdue invoice check and reminders --- #
    try:
        overdue_invoices = check_overdue(invoices)
        if overdue_invoices:
            for inv in overdue_invoices:
                body = generate_initial_reminder(inv['client'], inv['amount'], inv['due_date'])
                send_email(inv["email"], "Invoice Reminder", body)
                st.session_state["log_history"].append(f"[Reminder] Sent to {inv['client']} ({inv['email']}) for invoice due {inv['due_date']}")
        else:
            st.session_state["log_history"].append("[Reminder] No overdue invoices found.")
    except Exception as e:
        st.session_state["log_history"].append(f"[Error] Overdue check: {e}")
    # --- Inbox check and reply handling --- #
    try:
        replies = check_inbox(debug=False)
        if not replies:
            st.session_state["log_history"].append("[Inbox] No new replies found.")
        else:
            st.session_state["log_history"].append(f"[Inbox] Found {len(replies)} new email(s). Processing...")
            for sender, subject, content in replies:
                try:
                    if ai_should_escalate(content):
                        escalation_body = f"""
                        ⚠️ AI Agent Escalation Notice\n\nFrom: {sender}\nSubject: {subject}\nMessage:\n{content}
                        """
                        send_email(ESCALATION_EMAIL, f"⚠️ Escalation Needed: {subject}", escalation_body)
                        st.session_state["log_history"].append(f"[Escalation] Escalated to human team for {sender} | Subject: {subject}")
                        client_message = (
                            f"Dear Client,\n\nThank you for your message regarding '{subject}'.\nOur team has been notified and will get back to you shortly after reviewing your case.\n\nBest regards,\nInterprais Credit Team"
                        )
                        recipient = extract_email_address(sender)
                        send_email(recipient, f"Re: {subject}", client_message)
                        st.session_state["log_history"].append(f"[Escalation] Client notified at {recipient}")
                    else:
                        reply = generate_reply(content)
                        recipient = extract_email_address(sender)
                        send_email(recipient, "Re: " + subject, reply)
                        st.session_state["log_history"].append(f"[AI Reply] Sent to {recipient} | Subject: {subject}")
                except Exception as e:
                    st.session_state["log_history"].append(f"[Error] Processing reply from {sender}: {e}")
    except Exception as e:
        st.session_state["log_history"].append(f"[Error] Inbox check: {e}")

# --- Show the last 50 log entries --- #
with col2:
    log_lines = st.session_state["log_history"][-50:]
    log_placeholder.code("\n".join(log_lines), language="text")

# --- Invoice Table (Left Column) --- #
with col1:
    if invoices:
        st.subheader("🧾 Invoice List")
        st.dataframe(invoices, use_container_width=True)
    else:
        st.error("No invoice data found. Ensure 'invoices.json' exists.")

