import streamlit as st
import os
import json
from datetime import datetime
import smtplib
import imaplib
import email
from email.message import EmailMessage
from huggingface_hub import InferenceClient
from dotenv import load_dotenv

# --- Load environment variables --- #
load_dotenv()
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
ESCALATION_EMAIL = os.getenv("ESCALATION_EMAIL")
HF_TOKEN = os.getenv("HF_TOKEN")

# --- Hugging Face Model --- #
MODEL_NAME = "meta-llama/Llama-3.3-70B-Instruct"  # Or any other available model

# --- Email Config --- #
SMTP_SERVER = "smtp.gmail.com"
IMAP_SERVER = "imap.gmail.com"


# --- Load AI model --- #
@st.cache_resource
def load_model():
    return InferenceClient(api_key=HF_TOKEN)


client = load_model()


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
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = recipient
    msg.set_content(body)

    with smtplib.SMTP_SSL(SMTP_SERVER, 465) as smtp:
        smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        smtp.send_message(msg)

def ai_should_escalate(message_text):
    prompt = (
        f"Does the following client message require escalation to a human operator?\n\n"
        f"\"{message_text}\"\n\n"
        f"Reply with YES if a human should handle it, or NO if the AI can reply."
    )
    response = client.text_generation(prompt, model=MODEL_NAME, max_new_tokens=5).strip().lower()
    return "yes" in response


# --- Check inbox for replies --- #
def check_inbox(debug=False):
    with imaplib.IMAP4_SSL(IMAP_SERVER) as imap:
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
                                if payload_bytes:
                                    payload = payload_bytes.decode(errors="ignore")
                                    break
                    else:
                        payload_bytes = msg.get_payload(decode=True)
                        if payload_bytes:
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
    response = client.text_generation(prompt, model=MODEL_NAME, max_new_tokens=200)
    return response.strip()



# --- AI-generated invoice reminder --- #
def generate_initial_reminder(client_name, amount, due_date):
    prompt = (
        f"Write a professional, human-like email message from Interprais to {client_name}, reminding them that an invoice of ₦{amount} was due on {due_date}. "
        f"Be polite, respectful, and persuasive. Do not make up or assume any extra details about the invoice. "
        f"Encourage the client to acknowledge the reminder and provide a clear commitment for when payment will be made. "
        f"Keep it brief, warm, and straight to the point."
    )
    response = client.text_generation(prompt, model=MODEL_NAME, max_new_tokens=250)
    return response.strip()



# --- UI --- #
st.set_page_config(page_title="AI Credit Controller", page_icon="📧")
st.title("📧 AI Credit Controller")

invoices = load_invoices()
if invoices:
    st.subheader("🧾 Invoice List")
    st.dataframe(invoices, use_container_width=True)

    if st.button("🔍 Run Overdue Check & Email Clients"):
        overdue_invoices = check_overdue(invoices)
        if overdue_invoices:
            with st.spinner("Generating and sending emails..."):
                for inv in overdue_invoices:
                    body = generate_initial_reminder(inv['client'], inv['amount'], inv['due_date'])
                    send_email(inv["email"], "Invoice Reminder", body)
            st.success(f"Reminders sent to {len(overdue_invoices)} overdue clients.")
        else:
            st.info("No overdue invoices found.")
else:
    st.error("No invoice data found. Ensure 'invoices.json' exists.")

import re


def extract_email_address(full_address):
    match = re.search(r'<(.+?)>', full_address)
    if match:
        return match.group(1)
    return full_address.strip()

st.subheader("🤖 Handle Client Replies")

if st.button("📬 Fetch Replies & Generate AI Responses"):
    replies = check_inbox(debug=True)
    if not replies:
        st.info("No new replies found.")
    else:
        st.success(f"📨 Found {len(replies)} new email(s).")
        for sender, subject, content in replies:
            with st.expander(f"📧 From: {sender} | Subject: {subject}"):
                st.code(content, language="text")
                with st.spinner("Evaluating message..."):
                    try:
                        if ai_should_escalate(content):
                            # AI flags it for human review
                            escalation_body = f"""
                        ⚠️ AI Agent Escalation Notice

                        From: {sender}
                        Subject: {subject}

                        Message:
                        {content}
                        """
                            send_email(ESCALATION_EMAIL, f"⚠️ Escalation Needed: {subject}", escalation_body)
                            st.warning(f"🔔 Escalated to human team at {ESCALATION_EMAIL}")

                            # Notify client politely
                            client_message = (
                                f"Dear Client,\n\n"
                                f"Thank you for your message regarding \"{subject}\".\n"
                                f"Our team has been notified and will get back to you shortly after reviewing your case.\n\n"
                                f"Best regards,\nInterprais Credit Team"
                            )
                            recipient = extract_email_address(sender)
                            send_email(recipient, f"Re: {subject}", client_message)
                            st.info(f"📩 Client also notified at {recipient}")

                        else:
                            reply = generate_reply(content)
                            recipient = extract_email_address(sender)
                            send_email(recipient, "Re: " + subject, reply)
                            st.success(f"✅ AI Reply Sent to {recipient}")
                            st.code(reply)
                    except Exception as e:
                        st.error(f"❌ Failed to process message: {e}")

