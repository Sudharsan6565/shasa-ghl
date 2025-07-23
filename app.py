import os
import time
import json
import requests
import pytz
import re
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from flask import make_response
from flask_cors import CORS
from datetime import datetime


load_dotenv()
app = Flask(__name__)
CORS(app)

# GHL config
GHL_API_KEY = os.getenv("GHL_API_KEY")
GHL_CALENDAR_ID = os.getenv("GHL_CALENDAR_ID")
GHL_LOCATION_ID = os.getenv("Location_ID")
GHL_TIMEZONE = os.getenv("GHL_TIMEZONE", "America/Los_Angeles")
BASE_URL = os.getenv("GHL_BASE_URL", "https://rest.gohighlevel.com/v1")
TEAM_MEMBER_ID = os.getenv("TEAM_MEMBER_ID")

# VAPI config
VAPI_SECRET = os.getenv("VAPI_SECRET")
VAPI_CALL_URL = os.getenv("VAPI_CALL_URL")
VAPI_AGENT_ID = os.getenv("VAPI_AGENT_ID")
VAPI_HOOK_URL = os.getenv("VAPI_HOOK_URL")

ghl_headers = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Content-Type": "application/json"
}

def normalize_us_phone(phone):
    digits = re.sub(r"[^\d]", "", phone)
    if not digits.startswith("1"):
        digits = "1" + digits
    return f"+{digits}"

def classify_slot(dt: datetime) -> str:
    hour = dt.hour
    return "morning" if 8 <= hour < 12 else "afternoon"

@app.route("/slots", methods=["POST"])
def get_slots():
    from flask import current_app
    data = request.json or {}
    current_app.logger.info(f"[/slots] Incoming payload: {data}")

    try:
        message = data.get("message", {})
        tool_call = message.get("toolCalls", [{}])[0]
        arguments = tool_call.get("function", {}).get("arguments", {})

        start_ts = int(arguments.get("startDate"))
        end_ts = int(arguments.get("endDate"))
        tool_call_id = tool_call.get("id", "unknown")
    except (ValueError, TypeError, IndexError):
        return jsonify({
            "results": [
                {
                    "toolCallId": "unknown",
                    "error": "Invalid or missing startDate/endDate"
                }
            ]
        }), 200

    if start_ts < 1e11:
        start_ts *= 1000
    if end_ts < 1e11:
        end_ts *= 1000

    params = {
        "calendarId": GHL_CALENDAR_ID,
        "startDate": start_ts,
        "endDate": end_ts,
        "timezone": GHL_TIMEZONE,
        "userId": TEAM_MEMBER_ID
    }

    try:
        response = requests.get(
            f"{BASE_URL}/appointments/slots", headers=ghl_headers, params=params
        )
        slot_data = response.json()
    except Exception as e:
        return jsonify({
            "results": [
                {
                    "toolCallId": tool_call_id,
                    "error": "Failed to fetch or parse GHL response",
                    "details": str(e)
                }
            ]
        }), 200

    if response.status_code != 200:
        return jsonify({
            "results": [
                {
                    "toolCallId": tool_call_id,
                    "error": f"GHL API error: {slot_data}"
                }
            ]
        }), 200

    all_slots = []
    for _, val in slot_data.items():
        all_slots.extend(val.get("slots", []))

    grouped = {}
    fallback = []

    for iso_str in all_slots:
        try:
            dt = datetime.fromisoformat(iso_str)
            weekday = dt.strftime("%A")
            period = classify_slot(dt)

            if weekday not in grouped:
                grouped[weekday] = {"morning": [], "afternoon": []}

            grouped[weekday][period].append(iso_str)
            fallback.append(iso_str)
        except Exception as e:
            continue

    return jsonify({
        "results": [
            {
                "toolCallId": tool_call_id,
                "result": {
                    "grouped_slots": grouped,
                    "days": list(grouped.keys()),
                    "periods": ["morning", "afternoon"],
                    "fallback": fallback
                }
            }
        ]
    }), 200

@app.route("/book", methods=["POST"])
def book_appointment():
    payload = request.json
    print("[/book] Incoming payload:", payload)

    if "phone" not in payload or "startTime" not in payload:
        return jsonify({"error": "Missing required fields: 'phone' and/or 'startTime'"}), 400

    # Normalize phone
    payload["phone"] = normalize_us_phone(payload["phone"])

    booking_data = {
        "calendarId": GHL_CALENDAR_ID,
        "selectedSlot": payload["startTime"],
        "selectedTimezone": GHL_TIMEZONE,
        "phone": payload["phone"]
    }

    if "email" in payload:
        booking_data["email"] = payload["email"]
    if "name" in payload:
        booking_data["name"] = payload["name"]

    url = f"{BASE_URL}/appointments/"
    print("[/book] POST →", url)
    print("[/book] Payload →", json.dumps(booking_data, indent=2))

    try:
        res = requests.post(url, headers=ghl_headers, json=booking_data)
        data = res.json()
    except Exception as e:
        print("[BOOK EXCEPTION]", str(e))
        return jsonify({"error": "Booking request failed", "details": str(e)}), 500

    if res.status_code != 200:
        print("[BOOK ERROR]", data)
        return jsonify({"error": "Failed to book", "details": data}), 500

    print("[BOOK SUCCESS]", data)
    return jsonify(data)


@app.route("/webhook/lead", methods=["POST"])
def ghl_new_lead_webhook():
    data = request.json
    if not data or "phone" not in data:
        return jsonify({"error": "Missing required lead data"}), 400

    name = data.get("name", "Unknown")
    phone = normalize_us_phone(data["phone"])
    email = data.get("email", "ghost@fallback.ai")

    print(f"[GHL Lead] {name} - {phone} - {email}")

    # Trigger VAPI outbound call
    try:
        call_res = requests.post(VAPI_CALL_URL, headers={
            "Authorization": f"Bearer {VAPI_SECRET}",
            "Content-Type": "application/json"
        }, json={
            "agent_id": VAPI_AGENT_ID,
            "phone": phone,
            "metadata": {
                "name": name,
                "email": email,
                "source": "Facebook Lead"
            }
        })

        print("[VAPI] Call triggered:", call_res.status_code, call_res.text)
        return jsonify({"status": "VAPI call triggered", "vapi_response": call_res.json()})

    except Exception as e:
        print("[WEBHOOK EXCEPTION]", str(e))
        return jsonify({"error": "VAPI call failed", "details": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
