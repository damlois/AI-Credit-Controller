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
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

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
        f"As a professional credit controller, determine whether a client's reply "
        f"should be escalated to a human. Respond with only YES or NO.\n\n"
        f"Client's message:\n\"{message_text}\"\n\n"
        f"Say YES only if the client:\n"
        f"- Says they have already paid\n"
        f"- Expresses confusion or surprise about the message\n"
        f"- Submits a complaint or dispute\n\n"
        f"Say NO for all other responses, including if the client says they will pay later, "
        f"or provides information that can be handled without human involvement."
    )

    response = ollama_generate(prompt, max_tokens=5).lower()
    return "yes" in response

def ai_should_reply(message_text):
    prompt = (
        f"Determine if the following client message requires a reply from a credit controller. "
        f"Reply ONLY with YES or NO.\n\n"
        f"Message:\n\"{message_text}\"\n\n"
        f"Say NO only if the message is a simple, polite acknowledgment that clearly requires no further response "
        f"(such as 'Thank you', 'Noted', 'Okay', 'Received').\n\n"
        f"Say YES if the message:\n"
        f"- Mentions a future payment or states they will pay later\n"
        f"- Indicates they have already paid or will send proof of payment\n"
        f"- Expresses confusion, concern, or dissatisfaction\n"
        f"- Asks a question or seeks clarification\n"
        f"- Explains a reason for delay or requests more time\n"
        f"- Mentions a partial payment or any unusual situation\n"
        f"- Includes ambiguous or unclear content that might require follow-up"
    )

    response = ollama_generate(prompt, max_tokens=5).strip().lower()
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
        f"As a professional credit controller at Interprais, respond to the following client message:\n\n"
        f"\"{message_text}\"\n\n"
        f"Craft a single, thoughtful, human-like email reply based on the message. Choose only one appropriate response, "
        f"based on the client's intent.\n\n"
        f"- If the client says they will pay soon but doesn't give a date, thank them and ask for a specific payment date.\n"
        f"- If they already provided a date, acknowledge it politely.\n"
        f"- If they say they've already paid, apologize for any discrepancy and let them know you’ll verify and confirm.\n"
        f"- If they share a concern or delay, acknowledge it and request a realistic payment date.\n\n"
        f"Only reply with the one appropriate message — do not list multiple variations or explanations.**\n"
        f"Use a respectful and warm professional tone. Keep it concise and ready to send.\n"
        f"Do not include placeholders, commentary, or contact info.\n\n"
        f"Reply in this format:\n\n"
        f"Subject: [A short subject line]\n\n"
        f"Dear Customer,\n\n"
        f"[Email body]\n\n"
        f"Interprais Credit Controller"
    )

    return ollama_generate(prompt, max_tokens=350)




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
        # f"Thank you.\n\n"
        f"Interprais Credit Controller"
    )
    return ollama_generate(prompt, max_tokens=250)

# --- Extract email from header --- #
def extract_email_address(full_address):
    match = re.search(r'<(.+?)>', full_address)
    return match.group(1) if match else full_address.strip()

# --- UI --- #
st.set_page_config(page_title="AI Credit Controller", page_icon="📧")
st.title("📧 AI Credit Controller")
# --- Auto-refresh every 30 seconds --- #
# interval is in milliseconds, so 30000 = 30s
st_autorefresh(interval=30 * 1000, key="inbox_refresher")


if "log_history" not in st.session_state:
    st.session_state["log_history"] = []

# --- Load and display invoice stats --- #
invoices = load_invoices()
overdue_invoices = check_overdue(invoices)
overdue_count = len(overdue_invoices)

# --- Show Invoice Table --- #
st.subheader("🧾 Invoice List")
if invoices:
    st.dataframe(invoices, use_container_width=True)
else:
    st.error("No invoice data found. Ensure 'invoices.json' exists.")

col1, col2 = st.columns(2)

with col1:
    st.metric("Total Invoices", len(invoices))

with col2:
    st.metric("Overdue Invoices", overdue_count)

# --- Only send initial reminders once per session --- #
if "reminders_sent" not in st.session_state:
    try:
        if overdue_invoices:
            with st.spinner("📤 Sending payment reminders..."):
                for inv in overdue_invoices:
                    body = generate_initial_reminder(inv['client'], inv['amount'], inv['due_date'])
                    send_email(inv["email"], "Payment Reminder", body)
                    st.session_state["log_history"].append(
                        f"[Reminder] Sent to {inv['client']} ({inv['email']}) for invoice due {inv['due_date']}"
                    )
        else:
            st.session_state["log_history"].append("[Reminder] No overdue invoices found.")
        st.session_state["reminders_sent"] = True
    except Exception as e:
        st.session_state["log_history"].append(f"[Error] Overdue check: {e}")



# --- Show Activity Log --- #
st.subheader("📋 Activity Log")
log_lines = st.session_state["log_history"][-50:]
st.code("\n".join(log_lines), language="text")

with st.spinner("📬 Auto-checking inbox for replies..."):
    try:
        replies = check_inbox(debug=True)
        if not replies:
            st.session_state["log_history"].append("[Auto Inbox Check] No new replies.")
        else:
            st.session_state["log_history"].append(f"[Auto Inbox Check] Found {len(replies)} new email(s). Processing...")
            for sender, subject, content in replies:
                try:
                    if not ai_should_reply(content):
                        st.session_state["log_history"].append(
                            f"[No Reply Needed] Message from {sender} skipped: '{content[:60]}...'"
                        )
                        continue  # Skip further processing

                    if ai_should_escalate(content):
                        escalation_body = (
                            f"⚠️ AI Agent Escalation Notice\n\nFrom: {sender}\nSubject: {subject}\nMessage:\n{content}"
                        )
                        send_email(ESCALATION_EMAIL, f"⚠️ Escalation Needed: {subject}", escalation_body)
                        st.session_state["log_history"].append(
                            f"[Escalation] Escalated to human team for {sender} | Subject: {subject}"
                        )
                        client_message = (
                            f"Dear Customer,\n\nThank you for your message regarding '{subject}'.\n"
                            f"Our team has been notified and will get back to you shortly after reviewing your case.\n\n"
                            f"Interprais Credit Controller"
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
        st.session_state["log_history"].append(f"[Error] Auto inbox check: {e}")


