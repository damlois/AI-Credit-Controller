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

CONVERSATION_LOG_PATH = "conversation_history.json"

# On app load: delete old history and recreate
if "conversation_reset" not in st.session_state:
    if os.path.exists(CONVERSATION_LOG_PATH):
        os.remove(CONVERSATION_LOG_PATH)
    with open(CONVERSATION_LOG_PATH, "w") as f:
        json.dump({}, f)
    st.session_state["conversation_reset"] = True

def save_conversation(email_address, user_msg, ai_reply):
    try:
        with open(CONVERSATION_LOG_PATH, "r") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = {}

    conversation = history.get(email_address, [])
    conversation.append({"user": user_msg, "ai": ai_reply})
    history[email_address] = conversation

    with open(CONVERSATION_LOG_PATH, "w") as f:
        json.dump(history, f, indent=2)


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

# --- Load receipt data --- #
def load_receipts(file_path="receipts.json"):
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


# --- Check overdue invoices --- #
def check_overdue(invoices, paid_invoice_ids=None):
    if paid_invoice_ids is None:
        paid_invoice_ids = set()
    today = datetime.today().date()
    return [
        inv for inv in invoices
        # if inv["status"] == "unpaid"
        if inv["invoice_id"] not in paid_invoice_ids
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


def ai_should_reply(client_email, message_text):
    try:
        with open(CONVERSATION_LOG_PATH, "r") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = {}

    past_turns = history.get(client_email, [])
    conversation = ""
    for turn in past_turns[-5:]:  # limit to last 5 turns
        conversation += f"Client: {turn['user']}\n"
        conversation += f"AI: {turn['ai']}\n"
    conversation += f"Client: {message_text}\n"

    # prompt = (
    #     f"Determine if the following client message requires a reply from a credit controller. "
    #     f"Reply ONLY with YES or NO.\n\n"
    #     f"Message:\n\"{message_text}\"\n\n"
    #     f"Say NO only if the message is a simple, polite acknowledgment that clearly requires no further response "
    #     f"(such as 'Thank you', 'Noted', 'Okay', 'Received').\n\n"
    #     f"Say YES if the message:\n"
    #     f"- Mentions a future payment or states they will pay later\n"
    #     f"- Indicates they have already paid or will send proof of payment\n"
    #     f"- Expresses confusion, concern, or dissatisfaction\n"
    #     f"- Asks a question or seeks clarification\n"
    #     f"- Explains a reason for delay or requests more time\n"
    #     f"- Mentions a partial payment or any unusual situation\n"
    #     f"- Includes ambiguous or unclear content that might require follow-up"
    # )

    prompt = (
        f"Based on the following conversation between a client and a credit controller, "
        f"determine whether the most recent client message requires a reply. Respond ONLY with YES or NO.\n\n"
        f"{conversation}\n\n"
        f"Say NO only if the last message is a polite acknowledgment that clearly requires no further response, "
        f"such as 'Thanks', 'Noted', 'Okay', 'Alright', 'Received'.\n\n"
        f"Say YES if the message contains:\n"
        f"- Any question, ambiguity, or new information\n"
        f"- Mention of payment (e.g. 'I will pay tomorrow', 'Payment has been made', 'Can I pay next week?')\n"
        f"- A complaint, concern, delay, or explanation\n\n"
        f"Examples:\n"
        f"Client: Thanks –> NO\n"
        f"Client: Okay, noted –> NO\n"
        f"Client: I will pay later –> YES\n"
        f"Client: I have made the payment –> YES\n"
        f"Client: Can I pay next Friday? –> YES\n"
        f"Client: Sorry, I lost the invoice –> YES\n"
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
def generate_reply(client_email, new_message):
    # Load past conversation history
    try:
        with open(CONVERSATION_LOG_PATH, "r") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = {}

    past_turns = history.get(client_email, [])

    # Build message context
    conversation = ""
    for turn in past_turns[-5:]:  # Limit to last 5 exchanges to keep prompt concise
        conversation += f"Client: {turn['user']}\n"
        conversation += f"AI: {turn['ai']}\n"

    conversation += f"Client: {new_message}\nAI:"

    prompt = (
        f"As a professional credit controller at Interprais, respond to the client based on the conversation so far.\n\n"
        f"{conversation}\n\n"
        f"Craft a respectful and warm professional reply. Keep it concise and suitable to send as an email.\n\n"
        f"Use the following rules to determine your reply:\n"
        f"- If the client says they will pay soon but does not mention a clear day or date (e.g., just says 'soon' or 'next week'), thank them and politely ask when they expect to make the payment.\n"
        f"- Consider responses like 'today', 'tomorrow', 'on Friday', 'in 2 days', or '30th of June' as valid and specific **only if the date is today or in the future**. A time of day is not needed — just a clear day or date is sufficient.\n"
        f"- If the client mentions a date that is in the past, thank them and politely ask for the correct expected payment day or date in the future.\n"
        f"- If they give a valid future day or date, acknowledge it politely.\n"
        f"- If they say they've already paid, apologize for any discrepancy and let them know you’ll verify and confirm.\n"
        f"- If they share a concern or delay, acknowledge it and ask when they expect to be able to pay (again, day or date only).\n"
        f"- If their message is not related to the payment reminder, respond based on your best professional judgment and discretion.\n\n"
        f"Only reply with the one appropriate message — do not list multiple variations or explanations.\n"
        f"Do not include any placeholders, commentary, or contact information.\n"
        f"Do not include any detail you do not have such as invoice number, come up with a message that requires no edit and can be sent directly to the client\n"
        f"Respond in this format:\n\n"
        f"Subject: [A short subject line]\n\n"
        f"Dear Customer,\n\n"
        f"[Email body]\n\n"
        f"Interprais Credit Controller"
    )

    return ollama_generate(prompt, max_tokens=350)


# --- AI-generated invoice reminder --- #
def generate_initial_reminder(client_name, amount, due_date, invoice_no):
    prompt = (
        f"Write a concise, professional, and polite email reminder to {client_name} about their overdue invoice, with invoice number {invoice_no} of ₦{amount} due on {due_date}. "
        f"Politely ask when they expect to make the payment. "
        f"Do not include any reasoning, explanations, or extra commentary—just the email content. "
        f"Do not add a signature or company contact info. "
        f"Start directly with the greeting and end with a warm closing, but do not include your name or company details. "
        f"Reply in this format:\n\n"
        f"Subject: [A short subject line]\n\n"
        f"Dear {client_name},\n\n"
        f"[Email body]\n\n"
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
st_autorefresh(interval=30 * 1000, key="inbox_refresher")

if "log_history" not in st.session_state:
    st.session_state["log_history"] = []

# --- Load receipts and filter invoices --- #
# --- Load and display invoice stats --- #
invoices = load_invoices()
receipts = load_receipts()
paid_invoice_ids = {receipt["invoice_id"] for receipt in receipts}
filtered_invoices = [inv for inv in invoices if inv["invoice_id"] not in paid_invoice_ids]

overdue_invoices = check_overdue(invoices, paid_invoice_ids)
overdue_count = len(overdue_invoices)

st.subheader("🧾 Overdue Invoices")
if filtered_invoices:
    st.dataframe(filtered_invoices, use_container_width=True)
else:
    st.success("All invoices have corresponding receipts.")


# col1, col2 = st.columns(2)
#
# with col1:
#     st.metric("Total Invoices", len(invoices))
#
# with col2:
#     st.metric("Overdue Invoices", overdue_count)

# --- Only send initial reminders once per session --- #
if "reminders_sent" not in st.session_state:
    try:
        if overdue_invoices:
            with st.spinner("📤 Sending payment reminders..."):
                for inv in overdue_invoices:
                    body = generate_initial_reminder(inv['client'], inv['amount'], inv['due_date'], inv['invoice_id'])
                    send_email(inv["email"], "Payment Reminder", body)

                    # 🔁 Save initial conversation
                    save_conversation(inv["email"],
                                      "", body)

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
                try:
                    # recipient = extract_email_address(sender)
                    if not ai_should_reply(recipient, content):
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

                        send_email(recipient, f"Re: {subject}", client_message)
                        st.session_state["log_history"].append(f"[Escalation] Client notified at {recipient}")
                        save_conversation(recipient, content, client_message)
                    else:
                        # recipient = extract_email_address(sender)
                        reply = generate_reply(recipient, content)
                        send_email(recipient, "Re: " + subject, reply)
                        st.session_state["log_history"].append(f"[AI Reply] Sent to {recipient} | Subject: {subject}")
                        save_conversation(recipient, content, reply)
                except Exception as e:
                    st.session_state["log_history"].append(f"[Error] Processing reply from {sender}: {e}")

    except Exception as e:
        st.session_state["log_history"].append(f"[Error] Auto inbox check: {e}")
