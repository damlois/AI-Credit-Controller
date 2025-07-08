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

# --- Load environment variables ---
load_dotenv()
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
ESCALATION_EMAIL = os.getenv("ESCALATION_EMAIL")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

ollama_client = ollama.Client(host=OLLAMA_HOST)

CONVERSATION_LOG_PATH = "conversation_history.json"

# On app load: ensure conversation history file exists
if not os.path.exists(CONVERSATION_LOG_PATH):
    with open(CONVERSATION_LOG_PATH, "w") as f:
        json.dump({}, f)

def save_conversation(invoice_id, user_msg, ai_reply):
    try:
        with open(CONVERSATION_LOG_PATH, "r") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = {}

    conversation = history.get(invoice_id, [])
    conversation.append({"user": user_msg, "ai": ai_reply})
    history[invoice_id] = conversation

    with open(CONVERSATION_LOG_PATH, "w") as f:
        json.dump(history, f, indent=2)


def ollama_generate(prompt, model=None, max_tokens=250):
    if model is None:
        model = OLLAMA_MODEL
    response = ollama_client.generate(model=model, prompt=prompt, options={"num_predict": max_tokens})
    return response['response'].strip() if 'response' in response else "[No response from Ollama]"


# --- Load invoice data ---
def load_invoices(file_path="invoices.json"):
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []

# --- Load receipt data ---
def load_receipts(file_path="receipts.json"):
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


# --- Check overdue invoices ---
def check_overdue(invoices, paid_invoice_ids=None):
    if paid_invoice_ids is None:
        paid_invoice_ids = set()
    today = datetime.today().date()
    overdue_list = []
    for inv in invoices:
        if inv["invoice_id"] in paid_invoice_ids:
            continue

        due_date = datetime.strptime(inv["due_date"], "%Y-%m-%d").date()
        is_overdue_by_due_date = due_date < today

        promised_date = None
        if inv.get("promised_date"):
            try:
                promised_date = datetime.strptime(inv["promised_date"], "%Y-%m-%d").date()
            except ValueError:
                # Handle cases where promised_date might be in a different format or invalid
                promised_date = None

        if is_overdue_by_due_date:
            if promised_date is None or promised_date < today:
                overdue_list.append(inv)
    return overdue_list


# --- Send email ---
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

def ai_triage_client_reply(message_text):
    prompt = (
        f"As a credit controller, classify the client's message into one of the following categories: "
        f"'PROMISE_TO_PAY', 'PAID', 'DISPUTE', 'QUESTION', or 'IGNORE'.\n\n"
        f"Client's message:\n\"{message_text}\"\n\n"
        f"Classify as:\n"
        f"- 'PROMISE_TO_PAY': If the client says they will pay (e.g., 'I will pay tomorrow', 'I'll sort it next week').\n"
        f"- 'PAID': If the client claims they have already paid.\n"
        f"- 'DISPUTE': If the client disputes the charge, is confused, or complains.\n"
        f"- 'QUESTION': If the client asks a question that needs a reply.\n"
        f"- 'IGNORE': For simple acknowledgments (e.g., 'Okay', 'Noted', 'Thanks').\n\n"
        f"Respond with only the category name."
    )
    response = ollama_generate(prompt, max_tokens=15).strip().upper()
    # Ensure the response is one of the valid categories
    valid_categories = ['PROMISE_TO_PAY', 'PAID', 'DISPUTE', 'QUESTION', 'IGNORE']
    if response not in valid_categories:
        return 'QUESTION'  # Default to QUESTION if the response is not recognized
    return response

def update_invoice_promised_date(invoice_id, promised_date):
    with open("invoices.json", "r+") as f:
        invoices = json.load(f)
        for inv in invoices:
            if inv["invoice_id"] == invoice_id:
                inv["promised_date"] = promised_date
                break
        f.seek(0)
        json.dump(invoices, f, indent=2)
        f.truncate()

# --- Check inbox for replies ---
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


# --- AI-generated reply ---
def generate_reply(invoice_id, new_message, category='QUESTION', current_date=None):
    # Load past conversation history
    try:
        with open(CONVERSATION_LOG_PATH, "r") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = {}

    past_turns = history.get(invoice_id, [])

    # Build message context
    conversation = ""
    for turn in past_turns[-5:]:  # Limit to last 5 exchanges to keep prompt concise
        conversation += f"Client: {turn['user']}\n"
        conversation += f"AI: {turn['ai']}\n"

    conversation += f"Client: {new_message}\nAI:"

    # Base prompt
    prompt = (
        f"As a professional credit controller at Interprais, respond to the client based on the conversation so far.\n\n"
        f"{conversation}\n\n"
        f"Craft a respectful and warm professional reply. Keep it concise and suitable to send as an email.\n\n"
    )

    # Add category-specific instructions
    if category == 'PROMISE_TO_PAY':
        if current_date:
            prompt += f"Today's date is {current_date}. "
        prompt += (
            "The client has promised to pay. Your reply MUST include the promised date in YYYY-MM-DD format at the very end of the email body, like this: [PROMISED_DATE: YYYY-MM-DD]. "
            "If the client does not provide a specific date, you MUST state [PROMISED_DATE: NONE] at the very end of the email body. "
            "Example: If client says 'next Monday', and next Monday is 2025-07-14, your reply should end with [PROMISED_DATE: 2025-07-14]. "
            "Example: If client says 'soon', your reply should end with [PROMISED_DATE: NONE].\n"
        )
    elif category == 'PAID':
        prompt += "The client claims they have already paid. Apologize for the reminder and let them know you will verify the payment and get back to them.\n"
    elif category == 'DISPUTE':
        prompt += "The client is disputing the charge or is confused. Escalate this to a human. Let the client know that their message has been passed to the team for review.\n"
    elif category == 'QUESTION':
        prompt += "The client has a question. Answer it professionally based on the context.\n"

    prompt += (
        "Only reply with the one appropriate message — do not list multiple variations or explanations." 
        "Do not include any placeholders, commentary, or contact information.\n"
        "Do not include any detail you do not have such as invoice number, come up with a message that requires no edit and can be sent directly to the client\n"
        "Respond in this format:\n\n"
        "Dear Customer,\n\n"
        "[Email body]\n\n"
        "Interprais Credit Controller"
    )

    response_text = ollama_generate(prompt, max_tokens=350)

    # Extract subject and body
    subject_match = re.search(r"Subject: (.+?)\n", response_text)
    subject = subject_match.group(1).strip() if subject_match else "No Subject"

    body = response_text.split("\n\n", 1)[1].strip() if "\n\n" in response_text else response_text
    body = re.sub(r"Subject: .+\n\n", "", body, 1) # Remove subject line from body if present

    return subject, body


# --- AI-generated invoice reminder ---
def generate_initial_reminder(client_name, amount, due_date, invoice_no):
    prompt = (
        f"Write a concise, professional, and polite email reminder to {client_name} about their overdue invoice, with invoice number {invoice_no} of ₦{amount} due on {due_date}. "
        f"Politely ask when they expect to make the payment. "
        f"Do not include any reasoning, explanations, or extra commentary just the email content. "
        f"Do not add a signature or company contact info. "
        f"Start directly with the greeting and end with a warm closing, but do not include your name or company details. "
        f"Reply in this format:\n\n"
        f"[Email body]\n\n"
        f"Interprais Credit Controller"
    )
    return ollama_generate(prompt, max_tokens=250)



# --- Extract email from header ---
def extract_email_address(full_address):
    match = re.search(r'<(.+?)>', full_address)
    return match.group(1) if match else full_address.strip()


# --- UI ---
st.set_page_config(page_title="AI Credit Controller", page_icon="📧")
st.title("📧 AI Credit Controller")
# --- Auto-refresh every 30 seconds ---
st_autorefresh(interval=30 * 1000, key="inbox_refresher")

if "log_history" not in st.session_state:
    st.session_state["log_history"] = []

# --- Load receipts and filter invoices ---
# --- Load and display invoice stats ---
invoices = load_invoices()
receipts = load_receipts()
paid_invoice_ids = {receipt["invoice_id"] for receipt in receipts}
filtered_invoices = [inv for inv in invoices if inv["invoice_id"] not in paid_invoice_ids]

overdue_invoices = check_overdue(invoices, paid_invoice_ids)
overdue_count = len(overdue_invoices)

st.subheader("🧾 Overdue Invoices")
if overdue_invoices:
    st.dataframe(overdue_invoices, use_container_width=True)
else:
    st.success("No overdue invoices found based on due dates and promised dates.")


# col1, col2 = st.columns(2)
#
# with col1:
#     st.metric("Total Invoices", len(invoices))
#
# with col2:
#     st.metric("Overdue Invoices", overdue_count)

# --- Only send initial reminders if no conversation exists ---
if "reminders_sent" not in st.session_state:
    try:
        with open(CONVERSATION_LOG_PATH, "r") as f:
            history = json.load(f)
        existing_clients = history.keys()

        reminders_to_send = [
            inv for inv in overdue_invoices if inv["invoice_id"] not in existing_clients
        ]

        if reminders_to_send:
            with st.spinner(f"📤 Sending {len(reminders_to_send)} new payment reminders..."):
                for inv in reminders_to_send:
                    body = generate_initial_reminder(inv['client'], inv['amount'], inv['due_date'], inv['invoice_id'])
                    subject = f"Overdue Invoice Reminder (Invoice #{inv['invoice_id']})"
                    send_email(inv["email"], subject, body)

                    # 🔁 Save initial conversation
                    save_conversation(inv["invoice_id"], "", body)

                    st.session_state["log_history"].append(
                        f"[Reminder] Sent to {inv['client']} ({inv['email']}) for invoice {inv['invoice_id']} due {inv['due_date']}"
                    )
        else:
            st.session_state["log_history"].append("[Reminder] No new reminders to send.")

        st.session_state["reminders_sent"] = True
    except Exception as e:
        st.session_state["log_history"].append(f"[Error] Overdue check: {e}")

# --- Show Activity Log ---
st.subheader("📋 Activity Log")
log_lines = st.session_state["log_history"][-50:]
st.code("\n".join(log_lines), language="text")

with st.expander("🗂 View Conversation History"):
    try:
        with open(CONVERSATION_LOG_PATH, "r") as f:
            history = json.load(f)
        for client, exchanges in history.items():
            st.markdown(f"**{client}**")
            for turn in exchanges:
                st.markdown(f"- 🧑‍💼 Client: {turn['user']}")
                st.markdown(f"- 🤖 AI: {turn['ai']}")
                st.markdown("---")
    except Exception as e:
        st.error(f"Error loading conversation history: {e}")

with st.spinner("📬 Auto-checking inbox for replies..."):

    try:
        replies = check_inbox(debug=True)
        if not replies:
            st.session_state["log_history"].append("[Auto Inbox Check] No new replies.")
        else:
            st.session_state["log_history"].append(
                f"[Auto Inbox Check] Found {len(replies)} new email(s). Processing...")
            for sender, subject, content in replies:
                recipient = extract_email_address(sender)
                match = re.search(r"#([a-zA-Z0-9_,-]+)", subject)
                if not match:
                    st.session_state["log_history"].append(f"[Error] Could not extract invoice ID from subject: {subject}")
                    continue
                invoice_id = match.group(1)

                try:
                    category = ai_triage_client_reply(content)
                    st.session_state["log_history"].append(f"[AI Triage] Classified message from {recipient} for invoice {invoice_id} as {category}")

                    if category == 'IGNORE':
                        st.session_state["log_history"].append(
                            f"[No Reply Needed] Message from {sender} skipped: '{content[:60]}...'"
                        )
                        continue

                    if category == 'DISPUTE' or category == 'PAID':
                        escalation_body = (
                            f"⚠️ AI Agent Escalation Notice\n\n"
                            f"Invoice ID: {invoice_id}\n\n"
                            f"Category: {category}\n\n"
                            f"From: {sender}\n\n"
                            f"Subject: {subject}\n\n"
                            f"Message:\n{content}"
                        )
                        send_email(ESCALATION_EMAIL, f"⚠️ Escalation Needed: Invoice {invoice_id}", escalation_body)
                        st.session_state["log_history"].append(
                            f"[Escalation] Escalated to human team for invoice {invoice_id} from {sender} | Subject: {subject}"
                        )
                        subject, client_message = generate_reply(invoice_id, content, category, current_date=datetime.today().strftime('%Y-%m-%d'))
                        send_email(recipient, subject, client_message)
                        st.session_state["log_history"].append(f"[Escalation] Client notified at {recipient}")
                        save_conversation(invoice_id, content, client_message)
                    else: # PROMISE_TO_PAY or QUESTION
                        subject, reply = generate_reply(invoice_id, content, category, current_date=datetime.today().strftime('%Y-%m-%d'))
                        send_email(recipient, subject, reply)
                        st.session_state["log_history"].append(f"[AI Reply] Sent to {recipient} for invoice {invoice_id} | Subject: {subject}")
                        save_conversation(invoice_id, content, reply)

                        if category == 'PROMISE_TO_PAY':
                            # Extract promised date from AI reply
                            promised_date_match = re.search(r'\[PROMISED_DATE: (\d{4}-\d{2}-\d{2}|NONE)\]', reply)
                            if promised_date_match:
                                promised_date_str = promised_date_match.group(1)
                                if promised_date_str == 'NONE':
                                    promised_date = None
                                else:
                                    promised_date = promised_date_str
                                update_invoice_promised_date(invoice_id, promised_date)
                                st.session_state["log_history"].append(f"[Invoice Update] Updated promised date for invoice {invoice_id} to {promised_date}")
                            else:
                                st.session_state["log_history"].append(f"[Invoice Update] No specific promised date found in AI reply for invoice {invoice_id}")

                except Exception as e:
                    st.session_state["log_history"].append(f"[Error] Processing reply from {sender} for invoice {invoice_id}: {e}")

    except Exception as e:
        st.session_state["log_history"].append(f"[Error] Auto inbox check: {e}")
