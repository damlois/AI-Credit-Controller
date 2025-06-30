import streamlit as st
import os
import json
from datetime import datetime, timedelta
import smtplib
import imaplib
import email
from email.message import EmailMessage
from dotenv import load_dotenv
import ollama
import re
import time
import threading
import pandas as pd
from collections import defaultdict
import queue
import logging
from streamlit_autorefresh import st_autorefresh

# --- Setup logging --- #
logging.basicConfig(
    level=logging.DEBUG,  # Changed from INFO to DEBUG for more detailed logs
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("app.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# --- Load environment variables --- #
load_dotenv()
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
ESCALATION_EMAIL = os.getenv("ESCALATION_EMAIL")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

ollama_client = ollama.Client(host=OLLAMA_HOST)


# --- Global thread-safe data structures --- #
class ThreadSafeData:
    def __init__(self):
        self.lock = threading.Lock()
        self.log_history = []
        self.email_threads = defaultdict(list)
        self.invoice_status = {}
        self.workflow_running = True
        self.message_queue = queue.Queue()

    def add_log(self, message):
        with self.lock:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.log_history.append(f"[{timestamp}] {message}")
            # Keep only last 100 logs
            if len(self.log_history) > 100:
                self.log_history = self.log_history[-100:]

    def update_status(self, invoice_id, status):
        with self.lock:
            self.invoice_status[invoice_id] = status

    def add_email_thread(self, invoice_id, email_data):
        with self.lock:
            self.email_threads[invoice_id].append(email_data)

    def get_log_history(self):
        with self.lock:
            return self.log_history.copy()

    def get_email_threads(self):
        with self.lock:
            return dict(self.email_threads)

    def get_invoice_status(self):
        with self.lock:
            return self.invoice_status.copy()


# Global thread-safe data
thread_data = ThreadSafeData()


def ollama_generate(prompt, model=None, max_tokens=250):
    if model is None:
        model = OLLAMA_MODEL
    try:
        response = ollama_client.generate(
            model=model, prompt=prompt, think=False, options={"num_predict": max_tokens}
        )
        return (
            response["response"].strip()
            if "response" in response
            else "[No response from Ollama]"
        )
    except Exception as e:
        logger.error(f"Ollama error: {e}")
        return "[Error generating response]"


# --- Initialize Session State --- #
def init_session_state():
    if "initialized" not in st.session_state:
        st.session_state.initialed = True
        st.session_state.invoices = load_invoices()
        st.session_state.receipts = load_receipts()

        # Initialize invoice status
        for inv in st.session_state.invoices:
            thread_data.update_status(inv["invoice_id"], "Pending")

        # Start workflow if not already running
        if "workflow_started" not in st.session_state:
            st.session_state.workflow_started = True
            workflow_thread = threading.Thread(
                target=credit_control_workflow,
                daemon=True,
                name="CreditControlWorkflow",
            )
            workflow_thread.start()
            logger.info("Workflow thread started")


# --- Load invoice data --- #
def load_invoices(file_path="invoices.json"):
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"Invoice file {file_path} not found")
        return []
    except json.JSONDecodeError:
        logger.error(f"Invalid JSON in {file_path}")
        return []


# --- Load receipt data --- #
def load_receipts(file_path="receipts.json"):
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"Receipt file {file_path} not found")
        return []
    except json.JSONDecodeError:
        logger.error(f"Invalid JSON in {file_path}")
        return []


# --- Check overdue invoices with receipt verification --- #
def check_overdue(invoices, receipts):
    today = datetime.today().date()
    overdue = []

    for inv in invoices:
        # Skip paid invoices
        if inv.get("status") == "paid":
            continue

        try:
            due_date = datetime.strptime(inv["due_date"], "%Y-%m-%d").date()

            # Check if invoice is overdue
            if due_date < today:
                # Check if there's a matching receipt
                paid = any(r["invoice_id"] == inv["invoice_id"] for r in receipts)

                if not paid:
                    overdue.append(inv)
        except (ValueError, KeyError) as e:
            logger.error(
                f"Error processing invoice {inv.get('invoice_id', 'unknown')}: {e}"
            )
            continue

    return overdue


# --- Send email --- #
def send_email(recipient, subject, body, thread_id=None):
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        error_msg = "EMAIL_ADDRESS and EMAIL_PASSWORD must be set in the environment."
        logger.error(error_msg)
        thread_data.add_log(f"[Error] {error_msg}")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = recipient
    msg.set_content(body)

    # Generate a proper Message-ID with domain information for email threading
    domain = EMAIL_ADDRESS.split("@")[1] if "@" in EMAIL_ADDRESS else "example.com"
    message_id = (
        f"<{thread_id}@{domain}>"
        if thread_id
        else f"<{time.time()}.{hash(subject)}@{domain}>"
    )
    msg["Message-ID"] = message_id

    if thread_id:
        # For replies, use References and In-Reply-To headers with proper formatting
        reference_id = f"<{thread_id}@{domain}>"
        msg["References"] = reference_id
        msg["In-Reply-To"] = reference_id

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            smtp.send_message(msg)

        # Log email in thread
        if thread_id:
            thread_data.add_email_thread(
                thread_id,
                {
                    "sender": "System",
                    "subject": subject,
                    "body": body,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "message_id": message_id,  # Store the Message-ID for future reference
                },
            )

        logger.info(f"Email sent to {recipient}: {subject}")
        return True
    except Exception as e:
        error_msg = f"Error sending email to {recipient}: {str(e)}"
        logger.error(error_msg)
        thread_data.add_log(f"[Error] {error_msg}")
        return False


# --- AI decision functions --- #
def ai_should_escalate(message_text):
    prompt = (
        f"Determine if this client message should be escalated to a human agent. "
        f"Respond ONLY with YES or NO.\n\n"
        f'Message:\n"{message_text}"\n\n'
        f"Say YES if the client:\n"
        f"- Claims they have already paid\n"
        f"- Expresses confusion about the invoice\n"
        f"- Submits a complaint or dispute\n"
        f"- Requests special payment arrangements\n"
        f"- Mentions legal action\n\n"
        f"Say NO for all other responses."
    )
    response = ollama_generate(prompt, max_tokens=5).lower()
    return "yes" in response


def ai_should_reply(message_text):
    prompt = (
        f"Determine if this client message requires a reply. "
        f"Respond ONLY with YES or NO.\n\n"
        f'Message:\n"{message_text}"\n\n'
        f"Say YES if the message:\n"
        f"- Mentions a future payment date\n"
        f"- Asks a question or seeks clarification\n"
        f"- Explains a reason for delay\n"
        f"- Requests more information\n"
        f"- Contains any actionable content\n\n"
        f"Say NO if the message is a simple acknowledgment like 'Thank you' or 'Noted'."
    )
    response = ollama_generate(prompt, max_tokens=5).lower()
    return "yes" in response


# --- Check inbox for replies --- #
def check_inbox():
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        error_msg = "EMAIL_ADDRESS and EMAIL_PASSWORD must be set in the environment."
        logger.error(error_msg)
        return []

    replies = []
    try:
        with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
            imap.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            imap.select("inbox")

            # Search for UNSEEN messages first
            status, messages = imap.search(None, "(UNSEEN)")
            if status != "OK":
                logger.warning("Failed to search inbox for unseen messages")
                return []

            email_ids = messages[0].split()

            # If there are no unseen messages, also check for recent messages (last day)
            # This is useful if the user has already opened/seen the emails elsewhere
            if not email_ids:
                # Search for messages from the last 24 hours
                date = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
                status, messages = imap.search(None, f'(SINCE "{date}")')
                if status != "OK":
                    logger.warning("Failed to search inbox for recent messages")
                    return []
                email_ids = messages[0].split()

            logger.info(f"Found {len(email_ids)} email(s) to process")

            for num in email_ids:
                try:
                    status, msg_data = imap.fetch(num, "(RFC822)")
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])
                            subject = msg["subject"] or ""
                            from_ = msg["from"] or ""
                            message_id = msg.get("Message-ID", "")
                            in_reply_to = msg.get("In-Reply-To", "")
                            references = msg.get("References", "")

                            # Use either In-Reply-To or References for threading
                            thread_reference = (
                                in_reply_to if in_reply_to else references
                            )

                            # Extract email body
                            payload = ""
                            if msg.is_multipart():
                                for part in msg.walk():
                                    content_type = part.get_content_type()
                                    content_disp = str(part.get("Content-Disposition"))

                                    # Skip attachments
                                    if "attachment" in content_disp:
                                        continue

                                    if content_type == "text/plain":
                                        payload_bytes = part.get_payload(decode=True)
                                        if payload_bytes:
                                            payload = payload_bytes.decode(
                                                errors="ignore"
                                            )
                                            break
                                    elif content_type == "text/html" and not payload:
                                        # Use HTML content if no plain text is found
                                        payload_bytes = part.get_payload(decode=True)
                                        if payload_bytes:
                                            # Could use an HTML to text converter here for better formatting
                                            payload = payload_bytes.decode(
                                                errors="ignore"
                                            )
                            else:
                                payload_bytes = msg.get_payload(decode=True)
                                if payload_bytes:
                                    payload = payload_bytes.decode(errors="ignore")

                            # Remove email conversation history/quotes from reply
                            # This is a simple approach - could be improved
                            clean_payload = re.split(r"On .+ wrote:", payload)[0]
                            clean_payload = re.split(
                                r"\s*--+\s*Original Message\s*--+", clean_payload
                            )[0]
                            clean_payload = re.split(r"\s*From:.*Sent:", clean_payload)[
                                0
                            ]

                            if clean_payload.strip():
                                replies.append(
                                    {
                                        "from": from_,
                                        "subject": subject,
                                        "body": clean_payload.strip(),
                                        "message_id": message_id,
                                        "in_reply_to": thread_reference,
                                    }
                                )
                                logger.info(
                                    f"Processed reply from {from_}, subject: {subject}"
                                )
                            else:
                                logger.info(f"Skipping empty message from {from_}")

                            # Mark as seen unless already read
                            status, flags_data = imap.fetch(num, "(FLAGS)")
                            if b"\\Seen" not in flags_data[0]:
                                imap.store(num, "+FLAGS", "\\Seen")
                except Exception as e:
                    logger.error(f"Error processing email {num}: {e}")
                    continue

        return replies
    except Exception as e:
        error_msg = f"Error checking inbox: {str(e)}"
        logger.error(error_msg)
        thread_data.add_log(f"[Error] {error_msg}")
        return []


# --- AI-generated reply --- #
def generate_reply(message_text, client_name):
    prompt = (
        f"As a professional credit controller, respond to this message from {client_name}:\n\n"
        f'"{message_text}"\n\n'
        f"Guidelines:\n"
        f"- Be polite, professional, and concise\n"
        f"- Address the client's specific concerns\n"
        f"- Provide clear next steps if needed\n"
        f"- Keep response to 2-3 sentences\n\n"
        f"Reply format:\n"
        f"Subject: [Appropriate subject line]\n\n"
        f"[Email body]"
    )
    return ollama_generate(prompt, max_tokens=200)


# --- AI-generated invoice reminder --- #
def generate_initial_reminder(client_name, amount, due_date):
    prompt = (
        f"Write a professional payment reminder to {client_name} about their overdue invoice of ₦{amount} "
        f"that was due on {due_date}. Be polite but firm. Include:\n"
        f"- Clear mention of invoice details\n"
        f"- Request for immediate payment\n"
        f"- Offer to assist with questions\n\n"
        f"Reply format:\n"
        f"Subject: [Reminder subject]\n\n"
        f"[Email body]"
    )
    return ollama_generate(prompt, max_tokens=200)


# --- Extract email from header --- #
def extract_email_address(full_address):
    match = re.search(r"<(.+?)>", full_address)
    return match.group(1) if match else full_address.strip()


# --- Process email reply --- #
def process_email_reply(reply, invoices):
    try:
        email_address = extract_email_address(reply["from"])
        reply_to_id = reply["in_reply_to"] if reply["in_reply_to"] else None

        # First, try to extract invoice_id from In-Reply-To header
        invoice_id = None
        invoice = None

        if reply_to_id:
            # Extract possible invoice ID from the format <invoice_id@domain.com>
            match = re.search(r"<([^@]+)@", reply_to_id)
            if match:
                potential_id = match.group(1)
                logger.debug(
                    f"Extracted potential invoice ID from In-Reply-To: {potential_id}"
                )
                # Check if this ID exists in our invoice list
                for inv in invoices:
                    if inv["invoice_id"] == potential_id:
                        invoice_id = potential_id
                        invoice = inv
                        logger.debug(f"Matched invoice by ID: {invoice_id}")
                        break

        # If we couldn't find by In-Reply-To, try to match by email address
        if not invoice_id:
            for inv in invoices:
                if inv["email"].lower() == email_address.lower():
                    invoice = inv
                    invoice_id = inv["invoice_id"]
                    logger.debug(
                        f"Matched invoice by email address: {email_address} -> {invoice_id}"
                    )
                    break

        # If still not found, try to match by subject (look for invoice ID in subject)
        if not invoice_id:
            subject = reply["subject"].upper()
            for inv in invoices:
                if inv["invoice_id"] in subject:
                    invoice = inv
                    invoice_id = inv["invoice_id"]
                    logger.debug(
                        f"Matched invoice by subject: {subject} contains {invoice_id}"
                    )
                    break

        if not invoice_id:
            logger.warning(
                f"Could not match reply to any invoice. From: {email_address}, Subject: {reply['subject']}"
            )
            thread_data.add_log(
                f"Warning - Reply from unknown email: {email_address}, subject: {reply['subject']}"
            )
            return

        # Debug info
        logger.debug(f"Processing reply for invoice {invoice_id} from {email_address}")
        logger.debug(f"Reply subject: {reply['subject']}")
        logger.debug(f"Reply body length: {len(reply['body'])} chars")

        # Log received email
        thread_data.add_email_thread(
            invoice_id,
            {
                "sender": invoice["client"],
                "subject": reply["subject"],
                "body": reply["body"],
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

        thread_data.add_log(
            f"Email - Received from {invoice['client']} regarding invoice {invoice_id}"
        )
        thread_data.update_status(invoice_id, "Customer Replied")

        # Check if we should reply
        need_reply = ai_should_reply(reply["body"])
        logger.debug(f"AI determined reply needed: {need_reply}")

        if need_reply:
            # Check if escalation needed
            need_escalation = ai_should_escalate(reply["body"])
            logger.debug(f"AI determined escalation needed: {need_escalation}")

            if need_escalation:
                # Escalate to human
                if ESCALATION_EMAIL:
                    escalation_body = (
                        f"Client: {invoice['client']}\n"
                        f"Invoice: {invoice_id}\n"
                        f"Message:\n{reply['body']}"
                    )
                    if send_email(
                        ESCALATION_EMAIL,
                        f"ESCALATION: {reply['subject']}",
                        escalation_body,
                    ):
                        thread_data.update_status(invoice_id, "Escalated")
                        thread_data.add_log(
                            f"Escalation - Sent to human team for invoice {invoice_id}"
                        )
            else:
                # Generate and send AI reply
                ai_reply = generate_reply(reply["body"], invoice["client"])

                logger.debug(f"Generated AI reply: {ai_reply[:100]}...")

                # Extract subject and body
                subject_match = re.search(r"Subject:\s*(.+)", ai_reply)
                body_match = re.search(r"(?s)(Dear.+)", ai_reply)

                if subject_match and body_match:
                    subject = subject_match.group(1).strip()
                    email_body = body_match.group(1).strip()

                    if send_email(
                        invoice["email"],
                        subject,
                        email_body,
                        invoice_id,
                    ):
                        thread_data.update_status(invoice_id, "AI Replied")
                        thread_data.add_log(
                            f"AI Reply - Sent to {invoice['client']} for invoice {invoice_id}"
                        )
    except Exception as e:
        logger.exception(f"Error processing reply: {e}")
        thread_data.add_log(f"Error - Processing reply: {str(e)}")


# --- Main workflow function --- #
def credit_control_workflow():
    thread_data.add_log("System - Workflow started")

    # Load data once at start
    invoices = load_invoices()
    receipts = load_receipts()

    while thread_data.workflow_running:
        try:
            # Process overdue invoices (with receipt verification)
            overdue_invoices = check_overdue(invoices, receipts)

            for inv in overdue_invoices:
                invoice_id = inv["invoice_id"]
                current_status = thread_data.get_invoice_status().get(
                    invoice_id, "Pending"
                )

                if current_status in ["Pending", "Overdue"]:
                    try:
                        # Update status to overdue
                        thread_data.update_status(invoice_id, "Overdue")

                        # Generate and send reminder
                        body = generate_initial_reminder(
                            inv["client"], inv["amount"], inv["due_date"]
                        )

                        # Extract subject and body
                        subject_match = re.search(r"Subject:\s*(.+)", body)
                        body_match = re.search(r"(?s)(Dear.+)", body)

                        if subject_match and body_match:
                            subject = subject_match.group(1).strip()
                            email_body = body_match.group(1).strip()

                            if send_email(
                                inv["email"], subject, email_body, invoice_id
                            ):
                                thread_data.update_status(invoice_id, "Reminder Sent")
                                thread_data.add_log(
                                    f"Reminder - Sent to {inv['client']} ({inv['email']})"
                                )
                        else:
                            thread_data.add_log(
                                f"Error - Failed to parse reminder for {invoice_id}"
                            )
                    except Exception as e:
                        thread_data.add_log(
                            f"Error - Processing invoice {invoice_id}: {str(e)}"
                        )
                        logger.error(f"Error processing invoice {invoice_id}: {e}")

            # Process any messages from the queue (added manually via UI)
            while not thread_data.message_queue.empty():
                reply = thread_data.message_queue.get()
                logger.info(f"Processing queued message from {reply['from']}")
                process_email_reply(reply, invoices)

            # Check inbox and process replies
            replies = check_inbox()
            if replies:
                thread_data.add_log(f"Inbox - Found {len(replies)} new replies")

                for reply in replies:
                    process_email_reply(reply, invoices)

            time.sleep(30)  # Check every 30 seconds
        except Exception as e:
            thread_data.add_log(f"System Error - {str(e)}")
            logger.error(f"Workflow error: {e}")
            time.sleep(60)  # Wait longer on system errors


# --- UI Components --- #
def display_overdue_invoices():
    st.subheader("Overdue Invoices")

    # Get overdue invoices with receipt verification
    overdue_invoices = check_overdue(
        st.session_state.invoices, st.session_state.receipts
    )

    if not overdue_invoices:
        st.info("No overdue invoices found")
        return

    # Prepare data for display
    invoice_data = []
    status_dict = thread_data.get_invoice_status()

    for inv in overdue_invoices:
        status = status_dict.get(inv["invoice_id"], "Overdue")
        try:
            due_date = datetime.strptime(inv["due_date"], "%Y-%m-%d").date()
            overdue_days = (datetime.today().date() - due_date).days
        except ValueError:
            overdue_days = 0

        invoice_data.append(
            {
                "ID": inv["invoice_id"],
                "Client": inv["client"],
                "Amount": f"₦{inv['amount']}",
                "Due Date": inv["due_date"],
                "Overdue Days": overdue_days,
                "Status": status,
            }
        )

    st.dataframe(pd.DataFrame(invoice_data), use_container_width=True)


def display_activity_log():
    st.subheader("Activity Log")
    log_history = thread_data.get_log_history()

    if not log_history:
        st.info("No activities yet")
        return

    # Display last 20 activities
    recent_logs = log_history[-20:] if len(log_history) > 20 else log_history

    log_data = []
    for log in recent_logs:
        # Parse timestamp and message
        parts = log.split("] ", 1)
        if len(parts) == 2:
            timestamp = parts[0][1:]  # Remove opening bracket
            activity = parts[1]
        else:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            activity = log

        log_data.append({"Timestamp": timestamp, "Activity": activity})

    st.dataframe(pd.DataFrame(log_data), use_container_width=True, hide_index=True)


def display_email_threads():
    st.subheader("Email Threads")

    email_threads = thread_data.get_email_threads()

    if not email_threads:
        st.info("No email threads yet")
        return

    for invoice_id, messages in email_threads.items():
        with st.expander(f"Thread: {invoice_id} ({len(messages)} messages)"):
            for msg in sorted(messages, key=lambda x: x["timestamp"]):
                st.markdown(
                    f"""
                <div style="border-left: 3px solid {'#4e8cff' if msg['sender'] == 'System' else '#34a853'};
                            padding: 10px; margin-bottom: 15px; background: #f9f9f9; border-radius: 5px;">
                    <div style="font-weight: bold; color: {'#1a73e8' if msg['sender'] == 'System' else '#0d652d'}">
                        {msg['sender']}
                    </div>
                    <div style="font-size: 0.9em; color: #5f6368">{msg['timestamp']}</div>
                    <div style="margin-top: 5px; font-weight: bold">{msg['subject']}</div>
                    <div style="margin-top: 10px; white-space: pre-wrap;">{msg['body']}</div>
                </div>
                """,
                    unsafe_allow_html=True,
                )


# --- Status Summary --- #
def display_status_summary():
    # Get overdue invoices with receipt verification
    overdue_invoices = check_overdue(
        st.session_state.invoices, st.session_state.receipts
    )
    overdue_count = len(overdue_invoices)

    # Calculate status counts
    status_counts = {
        "Overdue": 0,
        "Reminder Sent": 0,
        "Customer Replied": 0,
        "AI Replied": 0,
        "Escalated": 0,
    }

    status_dict = thread_data.get_invoice_status()
    for status in status_dict.values():
        if status in status_counts:
            status_counts[status] += 1

    # Display metrics
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Overdue Invoices", overdue_count)
    with col2:
        st.metric("Reminders Sent", status_counts["Reminder Sent"])
    with col3:
        st.metric("Customer Replies", status_counts["Customer Replied"])
    with col4:
        st.metric("AI Replies", status_counts["AI Replied"])
    with col5:
        st.metric("Escalated", status_counts["Escalated"])


# --- UI --- #
def main():
    st.set_page_config(page_title="AI Credit Controller", page_icon="💰", layout="wide")
    st.title("AI-Powered Credit Controller")

    # Initialize session state and start workflow
    init_session_state()

    # Display status summary
    display_status_summary()
    st.divider()

    # Control buttons
    col1, col2, col3, col4 = st.columns([1, 1, 1, 3])
    with col1:
        if st.button("🔄 Refresh Data"):
            st.session_state.invoices = load_invoices()
            st.session_state.receipts = load_receipts()
            st.success("Data refreshed!")

    with col2:
        if st.button("📬 Check Inbox"):
            with st.spinner("Checking inbox..."):
                replies = check_inbox()
                if replies:
                    st.success(f"Found {len(replies)} new messages!")
                    # Add check_inbox replies to queue for processing
                    for reply in replies:
                        thread_data.message_queue.put(reply)
                else:
                    st.info("No new messages found.")

    with col3:
        if st.button("🛑 Stop Workflow"):
            thread_data.workflow_running = False
            st.warning("Workflow stopped!")

    # Display sections
    col1, col2 = st.columns([2, 1])

    with col1:
        display_overdue_invoices()

        # Real-time activity stream
        st.subheader("🚀 Real-time Activity Feed")
        log_history = thread_data.get_log_history()
        if log_history:
            last_5 = log_history[-5:]
            for log in reversed(last_5):
                if "Error" in log:
                    icon = "❌"
                    color = "#ea4335"
                elif "Escalation" in log:
                    icon = "⚠️"
                    color = "#fbbc05"
                elif "Email" in log or "Reply" in log:
                    icon = "✉️"
                    color = "#4285f4"
                elif "Reminder" in log:
                    icon = "📤"
                    color = "#34a853"
                else:
                    icon = "ℹ️"
                    color = "#5f6368"

                # Parse log message
                parts = log.split("] ", 1)
                if len(parts) == 2:
                    timestamp = parts[0][1:]
                    message = parts[1]
                else:
                    timestamp = "Unknown"
                    message = log

                st.markdown(
                    f"""
                <div style="display: flex; align-items: center; padding: 10px; border-bottom: 1px solid #eee;">
                    <span style="font-size: 1.5em; margin-right: 10px; color: {color}">{icon}</span>
                    <div>
                        <div style="font-weight: 500; color: {color}">[{timestamp}]</div>
                        <div>{message}</div>
                    </div>
                </div>
                """,
                    unsafe_allow_html=True,
                )
        else:
            st.info("No recent activities")

    with col2:
        display_activity_log()
        display_email_threads()

    # Auto-refresh the UI every 30 seconds
    # Add auto-refresh component
    st_autorefresh(interval=30000, key="autorefresh")

    # Add debug button at the bottom of the page
    expander = st.expander("⚙️ Debug Tools")
    with expander:
        if st.button("View Full Log"):
            with open("app.log", "r") as f:
                log_content = f.read()
                st.text_area("Full Application Log", log_content, height=500)

        # Add email configuration testing
        st.subheader("Email Configuration")
        if st.button("Test Email Configuration"):
            if EMAIL_ADDRESS and EMAIL_PASSWORD:
                try:
                    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                        smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                        st.success(
                            f"Successfully connected to SMTP server with {EMAIL_ADDRESS}"
                        )

                    with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
                        imap.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                        imap.select("inbox")
                        st.success(
                            f"Successfully connected to IMAP server with {EMAIL_ADDRESS}"
                        )
                except Exception as e:
                    st.error(f"Failed to connect: {str(e)}")
            else:
                st.error("EMAIL_ADDRESS or EMAIL_PASSWORD not set in environment")


if __name__ == "__main__":
    main()
