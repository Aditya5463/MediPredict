from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3, json, os
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

# ----------------------------------------------------
# App setup
# ----------------------------------------------------
app = Flask(__name__)
app.secret_key = "change_this_in_production_please"  # ⚠️ change for real use
DB_PATH = "database.db"

# ----------------------------------------------------
# DB helpers & bootstrap/migration
# ----------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def column_exists(cursor, table, column):
    cursor.execute(f"PRAGMA table_info({table});")
    cols = [r["name"] for r in cursor.fetchall()]
    return column in cols

def init_db():
    with get_db() as conn:
        c = conn.cursor()

        # users
        c.execute("""
          CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
          )
        """)

        # history
        c.execute("""
          CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            inputs_json TEXT,
            diabetes_risk TEXT,
            hypertension_risk TEXT,
            heart_risk TEXT,
            narrative TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
          )
        """)

        # safety migration: ensure columns exist (if DB created by old code)
        c.execute("PRAGMA table_info(history)")
        cols = [row["name"] for row in c.fetchall()]

        if "inputs_json" not in cols:
            c.execute("ALTER TABLE history ADD COLUMN inputs_json TEXT")
        if "diabetes_risk" not in cols:
            c.execute("ALTER TABLE history ADD COLUMN diabetes_risk TEXT")
        if "hypertension_risk" not in cols:
            c.execute("ALTER TABLE history ADD COLUMN hypertension_risk TEXT")
        if "heart_risk" not in cols:
            c.execute("ALTER TABLE history ADD COLUMN heart_risk TEXT")
        if "narrative" not in cols:
            c.execute("ALTER TABLE history ADD COLUMN narrative TEXT")

        conn.commit()

init_db()

# ----------------------------------------------------
# Utility & safety parsers (no external libs)
# ----------------------------------------------------
def s_float(v, d=0.0):
    try:
        return float(v)
    except:
        return d

def s_int(v, d=0):
    try:
        return int(float(v))
    except:
        return d

def compute_bmi(height_cm, weight_kg):
    h_m = height_cm / 100.0 if height_cm else 0
    if h_m <= 0:
        return 0.0
    return round(weight_kg / (h_m*h_m), 1)

# ----------------------------------------------------
# Risk scorers (rule-based “AI-ish”)
# ----------------------------------------------------
def score_diabetes(age, bmi, fasting_glucose, activity, fam_diabetes):
    score, reasons = 0, []

    if age >= 45:
        score += 2; reasons.append("Age ≥ 45 (+2)")
    if bmi >= 30:
        score += 3; reasons.append("BMI ≥ 30 (+3)")
    elif bmi >= 25:
        score += 1; reasons.append("BMI 25–29.9 (+1)")

    if fasting_glucose >= 126:
        score += 5; reasons.append("Fasting glucose ≥ 126 mg/dL (+5)")
    elif fasting_glucose >= 100:
        score += 3; reasons.append("Fasting glucose 100–125 mg/dL (+3)")

    if activity == "low":
        score += 2; reasons.append("Low activity (+2)")
    elif activity == "medium":
        score += 1; reasons.append("Moderate activity (+1)")

    if fam_diabetes:
        score += 2; reasons.append("Family history of diabetes (+2)")

    category = "High" if score >= 7 else "Moderate" if score >= 4 else "Low"
    return category, score, reasons

def score_hypertension(age, systolic, diastolic, bmi, smoker):
    score, reasons = 0, []

    if systolic >= 160 or diastolic >= 100:
        score += 5; reasons.append("Stage 2 BP (≥160/≥100) (+5)")
    elif systolic >= 140 or diastolic >= 90:
        score += 3; reasons.append("Stage 1 BP (≥140/≥90) (+3)")
    elif systolic >= 130 or diastolic >= 80:
        score += 2; reasons.append("Elevated BP (≥130/≥80) (+2)")

    if age >= 55:
        score += 2; reasons.append("Age ≥ 55 (+2)")
    if bmi >= 30:
        score += 2; reasons.append("BMI ≥ 30 (+2)")
    elif bmi >= 25:
        score += 1; reasons.append("BMI 25–29.9 (+1)")
    if smoker:
        score += 2; reasons.append("Smoker (+2)")

    category = "High" if score >= 7 else "Moderate" if score >= 4 else "Low"
    return category, score, reasons

def score_heart(age, gender, cholesterol, smoker, diabetes_cat, systolic):
    score, reasons = 0, []

    if (gender == "male" and age >= 45) or (gender == "female" and age >= 55):
        score += 2; reasons.append("Age threshold (+2)")

    if cholesterol >= 240:
        score += 3; reasons.append("Total cholesterol ≥ 240 (+3)")
    elif cholesterol >= 200:
        score += 2; reasons.append("Total cholesterol 200–239 (+2)")

    if smoker:
        score += 2; reasons.append("Smoker (+2)")

    if diabetes_cat == "High":
        score += 2; reasons.append("High diabetes risk (+2)")
    elif diabetes_cat == "Moderate":
        score += 1; reasons.append("Moderate diabetes risk (+1)")

    if systolic >= 140:
        score += 2; reasons.append("Systolic BP ≥ 140 (+2)")
    elif systolic >= 130:
        score += 1; reasons.append("Systolic BP 130–139 (+1)")

    category = "High" if score >= 7 else "Moderate" if score >= 4 else "Low"
    return category, score, reasons

def ai_guidance(inputs, res):
    """
    Unique feature: generate a patient-friendly summary with
    likely concerns + concrete actions, tailored to their numbers.
    """
    gender = inputs["gender"]
    age = inputs["age"]
    bmi = inputs["bmi"]
    systolic, diastolic = inputs["systolic"], inputs["diastolic"]
    fasting_glucose = inputs["fasting_glucose"]
    cholesterol = inputs["cholesterol"]
    smoker = inputs["smoker"]
    activity = inputs["activity"]

    parts = []
    parts.append(f"Based on your details (Age {age}, BMI {bmi}, BP {systolic}/{diastolic} mmHg, Fasting Glucose {fasting_glucose} mg/dL, Cholesterol {cholesterol} mg/dL), here’s a quick health check:")

    if res["diabetes"]["category"] != "Low":
        parts.append("• You may be at risk for **diabetes**. Consider checking HbA1c and fasting glucose with a clinician.")
    if res["hypertension"]["category"] != "Low":
        parts.append("• Your blood pressure profile suggests a **hypertension** risk. Home BP monitoring for 2–3 weeks is helpful.")
    if res["heart"]["category"] != "Low":
        parts.append("• Cardiovascular risk is elevated. Discuss a lipid profile and lifestyle plan with your clinician.")

    # Tailored suggestions
    tips = []
    # BMI-based
    if bmi >= 30:
        tips.append("Aim for gradual weight loss (5–7% in 3–6 months).")
    elif bmi < 18.5:
        tips.append("Your BMI is low — ensure adequate calories and protein; consider a nutrition consult.")

    # Glucose-based
    if fasting_glucose >= 126:
        tips.append("Fasting glucose is in diabetic range — seek medical evaluation soon.")
    elif fasting_glucose >= 100:
        tips.append("Fasting glucose is elevated — reduce refined sugar and increase fiber.")

    # BP-based
    if systolic >= 140 or diastolic >= 90:
        tips.append("Lower salt intake, manage stress, and check BP at home 3–4 days/week.")

    # Lipids
    if cholesterol >= 240:
        tips.append("Cholesterol is high — consider a lipid panel and Mediterranean-style diet.")
    elif cholesterol >= 200:
        tips.append("Borderline cholesterol — focus on unsaturated fats and regular exercise.")

    # Lifestyle
    if smoker:
        tips.append("Smoking cessation gives the biggest health win — consider a cessation plan.")
    if activity == "low":
        tips.append("Start with 30 minutes of brisk walking at least 5 days/week.")

    if not tips:
        tips.append("Keep up the great work! Maintain regular activity and balanced meals.")

    parts.append("**Care Tips:** " + " ".join(tips))
    parts.append("⚠️ This tool is educational and not a diagnosis. For symptoms or concerns, see a licensed clinician.")

    return "\n".join(parts)

# ----------------------------------------------------
# Routes
# ----------------------------------------------------
@app.route("/")
def index():
    # If already logged in, go to dashboard
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("index.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        if not username or not password:
            flash("Please fill both username and password.", "danger")
            return render_template("signup.html")

        with get_db() as conn:
            cur = conn.cursor()
            # Duplicate check (no redirect if duplicate)
            cur.execute("SELECT id FROM users WHERE username=?", (username,))
            exists = cur.fetchone()
            if exists:
                flash("⚠️ Username already exists. Try logging in.", "warning")
                return render_template("signup.html")
            # Create user
            cur.execute(
                "INSERT INTO users(username, password_hash) VALUES(?, ?)",
                (username, generate_password_hash(password))
            )
            conn.commit()

        flash("✅ Account created! Please login.", "success")
        return redirect(url_for("login"))

    return render_template("signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, username, password_hash FROM users WHERE username=?", (username,))
            user = cur.fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("dashboard"))
        flash("❌ Invalid credentials.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    result = None
    narrative = None

    if request.method == "POST":
        # Read inputs safely
        gender = request.form.get("gender", "male")
        age = s_int(request.form.get("age"))
        height_cm = s_float(request.form.get("height_cm"))
        weight_kg = s_float(request.form.get("weight_kg"))
        systolic = s_int(request.form.get("systolic"))
        diastolic = s_int(request.form.get("diastolic"))
        fasting_glucose = s_int(request.form.get("fasting_glucose"))
        cholesterol = s_int(request.form.get("cholesterol"))
        smoker = (request.form.get("smoker") == "yes")
        activity = request.form.get("activity", "low")
        fam_diabetes = (request.form.get("fam_diabetes") == "yes")

        bmi = compute_bmi(height_cm, weight_kg)

        # Score
        d_cat, d_score, d_reasons = score_diabetes(age, bmi, fasting_glucose, activity, fam_diabetes)
        h_cat, h_score, h_reasons = score_hypertension(age, systolic, diastolic, bmi, smoker)
        c_cat, c_score, c_reasons = score_heart(age, gender, cholesterol, smoker, d_cat, systolic)

        # Result object for template
        result = {
            "bmi": bmi,
            "diabetes": {"category": d_cat, "score": d_score, "reasons": d_reasons},
            "hypertension": {"category": h_cat, "score": h_score, "reasons": h_reasons},
            "heart": {"category": c_cat, "score": c_score, "reasons": c_reasons},
            "tips": []  # tips are embedded into narrative below to avoid duplication
        }

        inputs = {
            "gender": gender, "age": age, "height_cm": height_cm, "weight_kg": weight_kg,
            "bmi": bmi, "systolic": systolic, "diastolic": diastolic,
            "fasting_glucose": fasting_glucose, "cholesterol": cholesterol,
            "smoker": smoker, "activity": activity, "fam_diabetes": fam_diabetes
        }

        # AI-like narrative suggestions
        narrative = ai_guidance(inputs, result)

        # Save to DB
        with get_db() as conn:
            conn.execute("""
                INSERT INTO history(user_id, inputs_json, diabetes_risk, hypertension_risk, heart_risk, narrative)
                VALUES(?, ?, ?, ?, ?, ?)
            """, (session["user_id"], json.dumps(inputs), d_cat, h_cat, c_cat, narrative))
            conn.commit()

    return render_template("dashboard.html", result=result, narrative=narrative)

@app.route("/history")
def history():
    if "user_id" not in session:
        return redirect(url_for("login"))

    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, inputs_json, diabetes_risk, hypertension_risk, heart_risk, created_at, narrative
            FROM history
            WHERE user_id=?
            ORDER BY created_at DESC
        """, (session["user_id"],)).fetchall()

    records = []
    for r in rows:
        try:
            inputs = json.loads(r["inputs_json"]) if r["inputs_json"] else {}
        except:
            inputs = {}
        records.append({
            "id": r["id"],
            "created_at": r["created_at"],
            "diabetes": r["diabetes_risk"],
            "hypertension": r["hypertension_risk"],
            "heart": r["heart_risk"],
            "inputs": inputs,
            "narrative": r["narrative"] or ""
        })

    return render_template("history.html", records=records)

# ----------------------------------------------------
# Run
# ----------------------------------------------------
if __name__ == "__main__":
    # You can also set host="0.0.0.0" for LAN testing
    app.run(debug=True)
