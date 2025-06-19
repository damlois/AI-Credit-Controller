# 📧 AI Credit Controller – Interprais

An AI-powered email automation tool that acts as a credit controller for small businesses or finance teams. It scans for overdue invoices, sends reminders, and replies to client emails using a conversational AI model.

---

## 🚀 Features

- ✅ Automatically checks for overdue invoices
- ✅ Sends polite, persuasive payment reminders
- ✅ Reads client email replies via IMAP
- ✅ Uses LLMs (Llama or HuggingFace models) to generate smart responses
- ✅ Flags complex responses for human escalation
- ✅ Built with Python, Streamlit, and Hugging Face Inference API

---

## 🗂️ Project Structure

```

credit-controller/
│
├── app.py               # Main Streamlit app
├── invoices.json        # Invoice data
├── .env                 # Environment variables (not pushed)
├── requirements.txt     # Python dependencies
├── .gitignore           # Files to ignore in version control
└── README.md            # Project documentation

````

---

## ⚙️ Requirements

- Python 3.8+
- Hugging Face account with access token (for LLM)
- Email account with IMAP & SMTP enabled (e.g., Gmail)

---

## 🛠️ Setup

### 1. Clone the repo

```bash
git clone https://github.com/damlois/AI-Credit-Controller.git
cd CreditController
````

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure `.env`

Create a `.env` file in the root with your credentials:

```env
EMAIL_ADDRESS=your@email.com
EMAIL_PASSWORD=your-email-password
HF_TOKEN=your-huggingface-api-token
```

> For Gmail, you may need to use an [App Password](https://support.google.com/accounts/answer/185833) instead of your regular password.

---

## ▶️ Run the App

```bash
streamlit run app.py
```

The app will:

* Load your invoices
* Send reminders to overdue clients
* Fetch replies and respond via AI
* Escalate complex replies to your team

---

## 🧠 AI Models

This project uses Hugging Face’s Inference API with:

* `meta-llama/Llama-3.3-70B-Instruct` *(or any other available model)*
  You can replace this model in `app.py` based on availability.

