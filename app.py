from flask import Flask, render_template, request, redirect, url_for, session, abort, jsonify
import os
import pymysql
import json
import urllib.parse
import re
from functools import wraps
from collections import Counter
import math

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")


# ---------------- ADMIN AUTH ----------------
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if (
            username == os.environ.get("ADMIN_USERNAME")
            and password == os.environ.get("ADMIN_PASSWORD")
        ):
            session["is_admin"] = True
            return redirect(url_for("list_surveys"))

        return render_template("admin_login.html", error="Hatalı kullanıcı adı veya şifre")

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


# ---------------- DB ----------------
def get_db():
    """
    MYSQL_HOST bazen yanlış set edilince patlıyordu.
    Fallback zinciri: env -> mysql -> survey-project-mysql -> survey-project-mysql-1
    """
    host_env = os.environ.get("MYSQL_HOST")
    candidates = [h for h in [host_env, "mysql", "survey-project-mysql", "survey-project-mysql-1"] if h]

    last_err = None
    for host in candidates:
        try:
            return pymysql.connect(
                host=host,
                user=os.environ.get("MYSQL_USER", "root"),
                password=os.environ.get("MYSQL_PASSWORD", ""),
                database=os.environ.get("MYSQL_DB", "survey_app"),
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
            )
        except Exception as e:
            last_err = e
    raise last_err


# ---------------- Helpers ----------------
_TR_STOPWORDS = {
    "ve","veya","ile","da","de","bu","şu","o","ben","sen","biz","siz","onlar",
    "mi","mı","mu","mü","için","ama","fakat","ancak","çok","az","daha","en",
    "gibi","olarak","bir","iki","üç","dört","beş","ki","ne","neden","nasıl",
    "ya","yada","çünkü","hem","şey","şeyler"
}

_TURKISH_CHARS_RE = re.compile(r"[çğıöşüÇĞİÖŞÜ]")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

def _median(nums):
    nums = sorted(nums)
    n = len(nums)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return float(nums[mid])
    return (nums[mid - 1] + nums[mid]) / 2.0

def _std(nums):
    n = len(nums)
    if n <= 1:
        return 0.0
    mean = sum(nums) / n
    var = sum((x - mean) ** 2 for x in nums) / (n - 1)
    return math.sqrt(var)

def _tokenize_tr(text: str):
    text = (text or "").lower()
    text = re.sub(r"[^\w\sçğıöşü]", " ", text, flags=re.UNICODE)
    parts = [p.strip() for p in text.split() if p.strip()]
    parts = [p for p in parts if len(p) >= 3 and p not in _TR_STOPWORDS]
    return parts

def _looks_like_email_label(label: str) -> bool:
    lbl = (label or "").strip().lower()
    return ("email" in lbl) or ("e-mail" in lbl) or ("mail" in lbl)

def _validate_email(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return True
    if " " in v:
        return False
    if _TURKISH_CHARS_RE.search(v):
        return False
    if not _EMAIL_RE.match(v):
        return False
    return True


# ---------------- Routes ----------------
@app.route("/")
def home():
    return redirect(url_for("list_surveys"))


@app.route("/surveys")
@admin_required
def list_surveys():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                s.*,
                DATE_FORMAT(s.created_at, '%d/%m/%Y') AS created_date,
                COALESCE(q.question_count, 0) AS question_count,
                COALESCE(r.response_count, 0) AS response_count,
                d.avg_duration_min
            FROM surveys s
            LEFT JOIN (
                SELECT survey_id, COUNT(*) AS question_count
                FROM questions
                GROUP BY survey_id
            ) q ON q.survey_id = s.id
            LEFT JOIN (
                SELECT survey_id, COUNT(*) AS response_count
                FROM responses
                GROUP BY survey_id
            ) r ON r.survey_id = s.id
            LEFT JOIN (
                SELECT survey_id, ROUND(AVG(duration_seconds)/60, 0) AS avg_duration_min
                FROM participants
                WHERE duration_seconds IS NOT NULL
                GROUP BY survey_id
            ) d ON d.survey_id = s.id
            ORDER BY s.created_at DESC
        """)
        surveys = cur.fetchall()

    conn.close()

    total_questions = sum(int(s.get("question_count", 0) or 0) for s in surveys)
    total_responses = sum(int(s.get("response_count", 0) or 0) for s in surveys)

    return render_template(
        "survey_list.html",
        surveys=surveys,
        total_questions=total_questions,
        total_responses=total_responses
    )


@app.route("/surveys/new", methods=["GET", "POST"])
@admin_required
def create_survey():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        if not title:
            return render_template("create_survey.html"), 400

        # draft extra alanlar
        draft_labels = request.form.getlist("draft_label[]")
        draft_types = request.form.getlist("draft_type[]")
        draft_requireds = request.form.getlist("draft_required[]")
        draft_opts_json = request.form.getlist("draft_options_json[]")

        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO surveys (title, description) VALUES (%s, %s)",
                    (title, description)
                )
                survey_id = cur.lastrowid

                def insert_field(field_label, field_type, is_required, sort_order, options=None):
                    cur.execute(
                        """
                        INSERT INTO participant_fields (survey_id, field_label, field_type, is_required, sort_order, system_key)
                        VALUES (%s, %s, %s, %s, %s, NULL)
                        """,
                        (survey_id, field_label, field_type, is_required, sort_order)
                    )
                    field_id = cur.lastrowid
                    if options and field_type in ("single_choice", "multiple_choice"):
                        for idx, opt in enumerate(options, start=1):
                            cur.execute(
                                """
                                INSERT INTO participant_field_options (field_id, option_text, sort_order)
                                VALUES (%s, %s, %s)
                                """,
                                (field_id, opt, idx)
                            )
                    return field_id

                sort_order = 1

                for i in range(min(len(draft_labels), len(draft_types), len(draft_requireds), len(draft_opts_json))):
                    lbl = (draft_labels[i] or "").strip()
                    tp = (draft_types[i] or "text").strip()
                    req = 1 if str(draft_requireds[i]) == "1" else 0

                    if not lbl:
                        continue
                    if tp not in ("text", "single_choice", "multiple_choice"):
                        tp = "text"

                    try:
                        decoded = urllib.parse.unquote(draft_opts_json[i] or "")
                        opts = json.loads(decoded) if decoded else []
                    except Exception:
                        opts = []

                    opts = [str(o).strip() for o in (opts or []) if str(o).strip()]
                    if tp in ("single_choice", "multiple_choice") and len(opts) < 2:
                        continue

                    insert_field(lbl, tp, req, sort_order, options=opts)
                    sort_order += 1

                conn.commit()

            return redirect(url_for("edit_survey", survey_id=survey_id))

        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    return render_template("create_survey.html")


@app.route("/surveys/<int:survey_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_survey(survey_id):
    conn = get_db()

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM surveys WHERE id=%s", (survey_id,))
        survey = cur.fetchone()
        if not survey:
            conn.close()
            return "Anket bulunamadı", 404

    if request.method == "POST":
        form_type = request.form.get("form_type", "survey").strip()

        if form_type == "survey":
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            if title:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE surveys SET title=%s, description=%s WHERE id=%s",
                        (title, description, survey_id)
                    )
                    conn.commit()
            conn.close()
            return redirect(url_for("edit_survey", survey_id=survey_id))

        if form_type == "participant_field_add":
            field_label = request.form.get("field_label", "").strip()
            field_type = request.form.get("field_type", "text").strip()
            is_required = 1 if request.form.get("is_required") == "1" else 0

            option_texts = request.form.getlist("pf_options[]")
            option_texts = [o.strip() for o in option_texts if o.strip()]

            if field_label and field_type in ("text", "single_choice", "multiple_choice"):
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COALESCE(MAX(sort_order),0) AS mx FROM participant_fields WHERE survey_id=%s",
                        (survey_id,)
                    )
                    mx = int((cur.fetchone() or {}).get("mx") or 0)

                    cur.execute(
                        """
                        INSERT INTO participant_fields (survey_id, field_label, field_type, is_required, sort_order, system_key)
                        VALUES (%s, %s, %s, %s, %s, NULL)
                        """,
                        (survey_id, field_label, field_type, is_required, mx + 1)
                    )
                    field_id = cur.lastrowid

                    if field_type in ("single_choice", "multiple_choice"):
                        for idx, opt in enumerate(option_texts, start=1):
                            cur.execute(
                                """
                                INSERT INTO participant_field_options (field_id, option_text, sort_order)
                                VALUES (%s, %s, %s)
                                """,
                                (field_id, opt, idx)
                            )
                    conn.commit()

            conn.close()
            return redirect(url_for("edit_survey", survey_id=survey_id))

        conn.close()
        return redirect(url_for("edit_survey", survey_id=survey_id))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM participant_fields
            WHERE survey_id=%s
            ORDER BY sort_order ASC, id ASC
            """,
            (survey_id,)
        )
        participant_fields = cur.fetchall()

        for f in participant_fields:
            cur.execute(
                "SELECT * FROM participant_field_options WHERE field_id=%s ORDER BY sort_order ASC, id ASC",
                (f["id"],)
            )
            f["options"] = cur.fetchall()

    conn.close()
    return render_template("edit_survey.html", survey=survey, participant_fields=participant_fields)


@app.route("/surveys/<int:survey_id>/participant-fields/<int:field_id>/update", methods=["POST"])
@admin_required
def update_participant_field(survey_id, field_id):
    new_label = request.form.get("field_label", "").strip()
    is_required = 1 if request.form.get("is_required") == "1" else 0

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE participant_fields
            SET field_label=%s, is_required=%s
            WHERE id=%s AND survey_id=%s
            """,
            (new_label if new_label else None, is_required, field_id, survey_id)
        )
        conn.commit()
    conn.close()
    return redirect(url_for("edit_survey", survey_id=survey_id))


@app.route("/surveys/<int:survey_id>/participant-fields/<int:field_id>/delete", methods=["POST"])
@admin_required
def delete_participant_field(survey_id, field_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM participant_answers WHERE field_id=%s", (field_id,))
        cur.execute("DELETE FROM participant_field_options WHERE field_id=%s", (field_id,))
        cur.execute("DELETE FROM participant_fields WHERE id=%s AND survey_id=%s", (field_id, survey_id))
        conn.commit()
    conn.close()
    return redirect(url_for("edit_survey", survey_id=survey_id))


@app.route("/surveys/<int:survey_id>/questions", methods=["GET", "POST"])
@admin_required
def manage_questions(survey_id):
    conn = get_db()

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM surveys WHERE id=%s", (survey_id,))
        survey = cur.fetchone()
        if not survey:
            conn.close()
            return "Anket bulunamadı", 404

    if request.method == "POST":
        question_text = request.form.get("question_text", "").strip()
        question_type = request.form.get("question_type", "single_choice").strip()
        is_required = 1 if request.form.get("is_required") == "1" else 0

        option_texts = request.form.getlist("options[]")
        option_texts = [o.strip() for o in option_texts if o.strip()]

        scale_min = request.form.get("scale_min", "1").strip()
        scale_max = request.form.get("scale_max", "5").strip()
        min_label = request.form.get("scale_min_label", "").strip()
        max_label = request.form.get("scale_max_label", "").strip()

        if question_text:
            with conn.cursor() as cur:
                rating_min = None
                rating_max = None

                if question_type == "rating":
                    try:
                        mn = int(scale_min)
                        mx = int(scale_max)
                    except ValueError:
                        mn, mx = 1, 5
                    mn = max(1, mn)
                    mx = min(10, mx)
                    if mx < mn:
                        mn, mx = mx, mn
                    rating_min, rating_max = mn, mx

                cur.execute(
                    """
                    INSERT INTO questions (survey_id, question_text, question_type, is_required, rating_min, rating_max)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (survey_id, question_text, question_type, is_required, rating_min, rating_max)
                )
                question_id = cur.lastrowid

                if question_type in ("single_choice", "multiple_choice"):
                    for opt in option_texts:
                        cur.execute(
                            "INSERT INTO options (question_id, option_text, is_other) VALUES (%s, %s, 0)",
                            (question_id, opt)
                        )

                elif question_type == "rating":
                    mn = rating_min or 1
                    mx = rating_max or 5
                    for i in range(mn, mx + 1):
                        txt = str(i)
                        if i == mn and min_label:
                            txt = f"{i} - {min_label}"
                        if i == mx and max_label:
                            txt = f"{i} - {max_label}"
                        cur.execute(
                            "INSERT INTO options (question_id, option_text, is_other) VALUES (%s, %s, 0)",
                            (question_id, txt)
                        )

                conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM questions WHERE survey_id=%s ORDER BY id ASC", (survey_id,))
        questions = cur.fetchall()
        for q in questions:
            cur.execute("SELECT * FROM options WHERE question_id=%s ORDER BY id ASC", (q["id"],))
            q["options"] = cur.fetchall()

    conn.close()
    return render_template("manage_questions.html", survey=survey, questions=questions)


@app.route("/surveys/<int:survey_id>/questions/<int:question_id>/delete", methods=["POST"])
@admin_required
def delete_question(survey_id, question_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM answers WHERE question_id=%s", (question_id,))
        cur.execute("DELETE FROM options WHERE question_id=%s", (question_id,))
        cur.execute("DELETE FROM questions WHERE id=%s AND survey_id=%s", (question_id, survey_id))
        conn.commit()
    conn.close()
    return redirect(url_for("manage_questions", survey_id=survey_id))


@app.route("/surveys/<int:survey_id>/delete", methods=["POST"])
@admin_required
def delete_survey(survey_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM responses WHERE survey_id=%s", (survey_id,))
        resp_ids = [r["id"] for r in cur.fetchall()]

        if resp_ids:
            placeholders = ",".join(["%s"] * len(resp_ids))
            cur.execute(f"DELETE FROM answers WHERE response_id IN ({placeholders})", resp_ids)
            cur.execute(f"DELETE FROM responses WHERE id IN ({placeholders})", resp_ids)

        cur.execute("SELECT id FROM questions WHERE survey_id=%s", (survey_id,))
        qids = [r["id"] for r in cur.fetchall()]
        if qids:
            placeholders = ",".join(["%s"] * len(qids))
            cur.execute(f"DELETE FROM options WHERE question_id IN ({placeholders})", qids)
            cur.execute("DELETE FROM questions WHERE survey_id=%s", (survey_id,))

        cur.execute("SELECT id FROM participants WHERE survey_id=%s", (survey_id,))
        pids = [r["id"] for r in cur.fetchall()]
        if pids:
            placeholders = ",".join(["%s"] * len(pids))
            cur.execute(f"DELETE FROM participant_answers WHERE participant_id IN ({placeholders})", pids)
            cur.execute(f"DELETE FROM participants WHERE id IN ({placeholders})", pids)

        cur.execute("SELECT id FROM participant_fields WHERE survey_id=%s", (survey_id,))
        fids = [r["id"] for r in cur.fetchall()]
        if fids:
            placeholders = ",".join(["%s"] * len(fids))
            cur.execute(f"DELETE FROM participant_field_options WHERE field_id IN ({placeholders})", fids)
            cur.execute("DELETE FROM participant_fields WHERE survey_id=%s", (survey_id,))

        cur.execute("DELETE FROM surveys WHERE id=%s", (survey_id,))
        conn.commit()

    conn.close()
    return redirect(url_for("list_surveys"))


# ------------ PUBLIC: Take survey ------------
@app.route("/surveys/<int:survey_id>/take", methods=["GET", "POST"])
def take_survey(survey_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM surveys WHERE id = %s", (survey_id,))
        survey = cur.fetchone()
        if not survey:
            conn.close()
            return "Anket bulunamadı", 404

        cur.execute("SELECT * FROM questions WHERE survey_id=%s ORDER BY id ASC", (survey_id,))
        questions = cur.fetchall()
        for q in questions:
            cur.execute("SELECT * FROM options WHERE question_id=%s ORDER BY id ASC", (q["id"],))
            q["options"] = cur.fetchall()

        cur.execute(
            "SELECT * FROM participant_fields WHERE survey_id=%s ORDER BY sort_order ASC, id ASC",
            (survey_id,)
        )
        participant_fields = cur.fetchall()
        for f in participant_fields:
            cur.execute(
                "SELECT * FROM participant_field_options WHERE field_id=%s ORDER BY sort_order ASC, id ASC",
                (f["id"],)
            )
            f["options"] = cur.fetchall()

    if request.method == "POST":
        with conn.cursor() as cur:
            # 1) participant_fields validasyon
            for f in participant_fields:
                fid = f["id"]
                ftype = f["field_type"]
                required = int(f.get("is_required") or 0)
                label = f.get("field_label") or ""

                if ftype == "text":
                    val = request.form.get(f"pf_text_{fid}", "").strip()
                    if required and not val:
                        conn.rollback()
                        conn.close()
                        msg = f'"{label}" zorunlu.'
                        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                            return jsonify({"ok": False, "error": msg}), 400
                        return msg, 400

                    # email label ise email doğrula (doluysa)
                    if val and _looks_like_email_label(label) and (not _validate_email(val)):
                        conn.rollback()
                        conn.close()
                        msg = f'"{label}" geçerli bir e-mail olmalı (Türkçe karakter yok, @ ve doğru format).'
                        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                            return jsonify({"ok": False, "error": msg}), 400
                        return msg, 400

                elif ftype == "single_choice":
                    sel = request.form.get(f"pf_{fid}", "").strip()
                    if required and not sel:
                        conn.rollback()
                        conn.close()
                        msg = f'"{label}" zorunlu.'
                        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                            return jsonify({"ok": False, "error": msg}), 400
                        return msg, 400

                else:  # multiple_choice
                    sels = request.form.getlist(f"pf_{fid}")
                    if required and len(sels) == 0:
                        conn.rollback()
                        conn.close()
                        msg = f'"{label}" zorunlu (en az 1 seçim).'
                        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                            return jsonify({"ok": False, "error": msg}), 400
                        return msg, 400

            # 2) duration_seconds
            dur_raw = (request.form.get("duration_seconds") or "").strip()
            duration_seconds = None
            if dur_raw.isdigit():
                duration_seconds = int(dur_raw)
                if duration_seconds < 0:
                    duration_seconds = None
                if duration_seconds is not None and duration_seconds > 6 * 3600:
                    duration_seconds = None

            # 3) participants insert (artık zorunlu alan yok)
            cur.execute(
                """
                INSERT INTO participants (survey_id, first_name, last_name, email, duration_seconds)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (survey_id, None, None, None, duration_seconds)
            )
            participant_id = cur.lastrowid

            # 4) participant_answers kaydet (tüm participant_fields)
            for f in participant_fields:
                fid = f["id"]
                ftype = f["field_type"]
                label = f.get("field_label") or ""

                if ftype == "text":
                    val = request.form.get(f"pf_text_{fid}", "").strip()
                    if val:
                        cur.execute(
                            """
                            INSERT INTO participant_answers (participant_id, field_id, option_id, answer_text)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (participant_id, fid, None, val)
                        )

                elif ftype == "single_choice":
                    sel = request.form.get(f"pf_{fid}", "").strip()
                    if sel.isdigit():
                        cur.execute(
                            """
                            INSERT INTO participant_answers (participant_id, field_id, option_id, answer_text)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (participant_id, fid, int(sel), None)
                        )

                else:  # multiple_choice
                    sels = request.form.getlist(f"pf_{fid}")
                    for s in sels:
                        if str(s).isdigit():
                            cur.execute(
                                """
                                INSERT INTO participant_answers (participant_id, field_id, option_id, answer_text)
                                VALUES (%s, %s, %s, %s)
                                """,
                                (participant_id, fid, int(s), None)
                            )

            # 5) response
            cur.execute(
                "INSERT INTO responses (survey_id, participant_id) VALUES (%s, %s)",
                (survey_id, participant_id)
            )
            response_id = cur.lastrowid

            # 6) soru cevapları
            for q in questions:
                qid = q["id"]
                qtype = q.get("question_type", "single_choice")
                q_required = int(q.get("is_required") or 0)

                if qtype == "text":
                    text_val = request.form.get(f"question_text_{qid}", "").strip()
                    if q_required and not text_val:
                        conn.rollback()
                        conn.close()
                        msg = f'"{q["question_text"]}" sorusu zorunlu.'
                        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                            return jsonify({"ok": False, "error": msg}), 400
                        return msg, 400
                    if text_val:
                        cur.execute(
                            """
                            INSERT INTO answers (response_id, question_id, option_id, answer_text, answer_number)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (response_id, qid, None, text_val, None)
                        )

                elif qtype == "rating":
                    raw = request.form.get(f"question_{qid}", "").strip()
                    if q_required and not raw:
                        conn.rollback()
                        conn.close()
                        msg = f'"{q["question_text"]}" sorusu zorunlu.'
                        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                            return jsonify({"ok": False, "error": msg}), 400
                        return msg, 400

                    token = raw.split()[0] if raw else ""
                    if token.isdigit():
                        rating_num = int(token)
                        if 1 <= rating_num <= 10:
                            cur.execute(
                                """
                                INSERT INTO answers (response_id, question_id, option_id, answer_text, answer_number)
                                VALUES (%s, %s, %s, %s, %s)
                                """,
                                (response_id, qid, None, None, rating_num)
                            )

                elif qtype == "multiple_choice":
                    selected_ids = request.form.getlist(f"question_{qid}")
                    if q_required and len(selected_ids) == 0:
                        conn.rollback()
                        conn.close()
                        msg = f'"{q["question_text"]}" sorusu zorunlu (en az 1 seçim).'
                        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                            return jsonify({"ok": False, "error": msg}), 400
                        return msg, 400

                    for sid in selected_ids:
                        if not str(sid).isdigit():
                            continue
                        opt_id = int(sid)

                        is_other = any(
                            o["id"] == opt_id and int(o.get("is_other", 0)) == 1
                            for o in q.get("options", [])
                        )
                        other_text = (request.form.get(f"other_{qid}", "").strip() or None) if is_other else None

                        cur.execute(
                            """
                            INSERT INTO answers (response_id, question_id, option_id, answer_text, answer_number)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (response_id, qid, opt_id, other_text, None)
                        )

                else:  # single_choice
                    selected_option_id = request.form.get(f"question_{qid}", "").strip()
                    if q_required and not selected_option_id:
                        conn.rollback()
                        conn.close()
                        msg = f'"{q["question_text"]}" sorusu zorunlu.'
                        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                            return jsonify({"ok": False, "error": msg}), 400
                        return msg, 400

                    if selected_option_id.isdigit():
                        opt_id = int(selected_option_id)

                        is_other = any(
                            o["id"] == opt_id and int(o.get("is_other", 0)) == 1
                            for o in q.get("options", [])
                        )
                        other_text = (request.form.get(f"other_{qid}", "").strip() or None) if is_other else None

                        cur.execute(
                            """
                            INSERT INTO answers (response_id, question_id, option_id, answer_text, answer_number)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (response_id, qid, opt_id, other_text, None)
                        )

            conn.commit()

        conn.close()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": True})

        return redirect(url_for("take_survey", survey_id=survey_id))

    conn.close()
    return render_template(
        "take_survey.html",
        survey=survey,
        questions=questions,
        participant_fields=participant_fields
    )


# ------------ Results (admin) ------------
@app.route("/surveys/<int:survey_id>/results")
@admin_required
def show_results(survey_id):
    participant_id = request.args.get("participant_id", "").strip()
    pid_int = int(participant_id) if participant_id.isdigit() else None

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM surveys WHERE id=%s", (survey_id,))
        survey = cur.fetchone()
        if not survey:
            conn.close()
            return "Anket bulunamadı", 404

        cur.execute(
            """
            SELECT id, first_name, last_name, email, created_at
            FROM participants
            WHERE survey_id=%s
            ORDER BY id DESC
            """,
            (survey_id,)
        )
        participants = cur.fetchall()

        selected_participant = None
        participant_answers_map = {"choice": {}, "text": {}, "rating": {}}

        if pid_int:
            cur.execute("SELECT * FROM participants WHERE id=%s AND survey_id=%s", (pid_int, survey_id))
            selected_participant = cur.fetchone()

            if selected_participant:
                cur.execute(
                    """
                    SELECT r.id AS response_id
                    FROM responses r
                    WHERE r.survey_id=%s AND r.participant_id=%s
                    ORDER BY r.id DESC
                    LIMIT 1
                    """,
                    (survey_id, pid_int)
                )
                rrow = cur.fetchone()
                response_id = rrow["response_id"] if rrow else None

                if response_id:
                    cur.execute(
                        """
                        SELECT question_id, option_id, answer_text, answer_number
                        FROM answers
                        WHERE response_id=%s
                        """,
                        (response_id,)
                    )
                    rows = cur.fetchall()
                    for a in rows:
                        qid = a["question_id"]
                        if a["option_id"] is not None:
                            participant_answers_map["choice"].setdefault(qid, set()).add(int(a["option_id"]))
                        if a["answer_text"]:
                            participant_answers_map["text"][qid] = a["answer_text"]
                        if a["answer_number"] is not None:
                            participant_answers_map["rating"][qid] = int(a["answer_number"])

        cur.execute("SELECT * FROM questions WHERE survey_id=%s ORDER BY id ASC", (survey_id,))
        questions = cur.fetchall()

        for q in questions:
            qid = q["id"]
            qtype = q.get("question_type", "single_choice")

            cur.execute("SELECT * FROM options WHERE question_id=%s ORDER BY id ASC", (qid,))
            q["options"] = cur.fetchall()

            if qtype in ("single_choice", "multiple_choice"):
                cur.execute(
                    """
                    SELECT o.id, o.option_text, o.is_other,
                           COUNT(a.id) AS vote_count
                    FROM options o
                    LEFT JOIN answers a
                        ON a.option_id = o.id AND a.question_id = %s
                    WHERE o.question_id = %s
                    GROUP BY o.id, o.option_text, o.is_other
                    ORDER BY o.id ASC
                    """,
                    (qid, qid)
                )
                options = cur.fetchall()

                total_votes = sum(int(o["vote_count"]) for o in options)
                divisor = total_votes or 1
                for o in options:
                    o["percent"] = round(int(o["vote_count"]) * 100 / divisor, 1)

                q["options_stats"] = options
                q["total_votes"] = total_votes

                cur.execute(
                    """
                    SELECT answer_text
                    FROM answers
                    WHERE question_id=%s AND answer_text IS NOT NULL AND answer_text <> ''
                    ORDER BY id DESC
                    LIMIT 50
                    """,
                    (qid,)
                )
                all_texts = [r["answer_text"] for r in cur.fetchall()]

                sel_text = participant_answers_map["text"].get(qid)
                if sel_text:
                    all_texts = [sel_text] + [t for t in all_texts if t != sel_text]
                q["other_texts"] = all_texts[:20]

            elif qtype == "rating":
                cur.execute(
                    """
                    SELECT COUNT(*) AS cnt, AVG(answer_number) AS avg_rating
                    FROM answers
                    WHERE question_id=%s AND answer_number IS NOT NULL
                    """,
                    (qid,)
                )
                agg = cur.fetchone() or {"cnt": 0, "avg_rating": None}
                q["rating_count"] = int(agg["cnt"] or 0)
                q["rating_avg"] = round(float(agg["avg_rating"]), 2) if agg["avg_rating"] is not None else None

                cur.execute(
                    """
                    SELECT answer_number AS rating, COUNT(*) AS cnt
                    FROM answers
                    WHERE question_id=%s AND answer_number IS NOT NULL
                    GROUP BY answer_number
                    ORDER BY answer_number ASC
                    """,
                    (qid,)
                )
                dist = cur.fetchall()
                dist_map = {int(r["rating"]): int(r["cnt"]) for r in dist}

                mn = int(q.get("rating_min") or 1)
                mx = int(q.get("rating_max") or 5)
                mn = max(1, mn)
                mx = min(10, mx)
                if mx < mn:
                    mx = mn

                q["rating_distribution"] = [{"rating": i, "cnt": dist_map.get(i, 0)} for i in range(mn, mx + 1)]

            else:  # text
                cur.execute(
                    """
                    SELECT answer_text
                    FROM answers
                    WHERE question_id=%s AND answer_text IS NOT NULL AND answer_text <> ''
                    ORDER BY id DESC
                    LIMIT 50
                    """,
                    (qid,)
                )
                texts = [r["answer_text"] for r in cur.fetchall()]

                sel_text = participant_answers_map["text"].get(qid)
                if sel_text:
                    texts = [sel_text] + [t for t in texts if t != sel_text]

                q["text_answers"] = texts
                q["text_count"] = len(texts)

    conn.close()
    return render_template(
        "results.html",
        survey=survey,
        questions=questions,
        participants=participants,
        selected_participant=selected_participant,
        participant_answers_map=participant_answers_map
    )


# ------------ Analytics (admin) ------------
def build_question_analytics(conn, survey_id: int, participant_count: int = 0):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, question_text, question_type
            FROM questions
            WHERE survey_id=%s
            ORDER BY id ASC
        """, (survey_id,))
        questions = cur.fetchall()

        qstats = []

        for q in questions:
            qid = q["id"]
            qtype = q["question_type"]

            cur.execute("""
                SELECT COUNT(DISTINCT a.response_id) AS responders
                FROM answers a
                JOIN responses r ON r.id = a.response_id
                WHERE r.survey_id=%s AND a.question_id=%s
            """, (survey_id, qid))
            responders = (cur.fetchone() or {}).get("responders", 0) or 0

            item = {
                "id": qid,
                "question_text": q["question_text"],
                "question_type": qtype,
                "responders": responders,
                "missing": max((participant_count or 0) - responders, 0),
                "options": [],
                "most": None,
                "least": None,
                "rating": None,
                "text": None,
            }

            if qtype in ("single_choice", "multiple_choice"):
                cur.execute("""
                    SELECT id, option_text
                    FROM options
                    WHERE question_id=%s
                    ORDER BY id ASC
                """, (qid,))
                opts = cur.fetchall()

                cur.execute("""
                    SELECT a.option_id, COUNT(*) AS cnt
                    FROM answers a
                    JOIN responses r ON r.id = a.response_id
                    WHERE r.survey_id=%s AND a.question_id=%s AND a.option_id IS NOT NULL
                    GROUP BY a.option_id
                """, (survey_id, qid))
                rows = cur.fetchall()

                counts = {o["id"]: 0 for o in opts}
                for r in rows:
                    oid = r.get("option_id")
                    if oid in counts:
                        counts[oid] = int(r.get("cnt") or 0)

                base = responders if qtype == "single_choice" else (sum(counts.values()) or 0)

                opt_rows = []
                for o in opts:
                    cnt = int(counts.get(o["id"], 0))
                    pct = (cnt / base * 100.0) if base else 0.0
                    opt_rows.append({
                        "id": o["id"],
                        "text": o["option_text"],
                        "count": cnt,
                        "pct": round(pct, 1),
                    })

                item["options"] = opt_rows
                if opt_rows:
                    item["most"] = max(opt_rows, key=lambda x: x["count"])
                    item["least"] = min(opt_rows, key=lambda x: x["count"])

            elif qtype == "rating":
                vals = []
                cur.execute("""
                    SELECT a.answer_number, a.answer_text
                    FROM answers a
                    JOIN responses r ON r.id = a.response_id
                    WHERE r.survey_id=%s AND a.question_id=%s
                """, (survey_id, qid))
                rows = cur.fetchall()

                for r in rows:
                    v = r.get("answer_number")
                    if v is None:
                        t = (r.get("answer_text") or "").strip()
                        if t.isdigit():
                            v = int(t)
                    if v is not None:
                        try:
                            vals.append(float(v))
                        except Exception:
                            pass

                if vals:
                    mean = sum(vals) / len(vals)
                    med = _median(vals)
                    sd = _std(vals)
                    dist = Counter(int(x) for x in vals)
                    item["rating"] = {
                        "n": len(vals),
                        "mean": round(mean, 2),
                        "median": round(med, 2) if med is not None else None,
                        "std": round(sd, 2),
                        "dist": [{"score": k, "count": dist[k]} for k in sorted(dist.keys())],
                    }
                else:
                    item["rating"] = {"n": 0, "mean": None, "median": None, "std": None, "dist": []}

            elif qtype == "text":
                cur.execute("""
                    SELECT a.answer_text
                    FROM answers a
                    JOIN responses r ON r.id = a.response_id
                    WHERE r.survey_id=%s AND a.question_id=%s
                      AND a.answer_text IS NOT NULL
                      AND TRIM(a.answer_text) <> ''
                """, (survey_id, qid))
                rows = cur.fetchall()
                texts = [rr["answer_text"] for rr in rows if rr.get("answer_text")]

                n = len(texts)
                avg_len = round(sum(len(t) for t in texts) / n, 1) if n else 0.0

                tokens = []
                for t in texts:
                    tokens.extend(_tokenize_tr(t))
                top_words = Counter(tokens).most_common(20)

                item["text"] = {
                    "n": n,
                    "avg_len": avg_len,
                    "top_words": [{"w": w, "c": c} for (w, c) in top_words],
                }

            qstats.append(item)

        return qstats


@app.route("/analytics")
@admin_required
def analytics():
    survey_id = request.args.get("survey_id", type=int)
    view = request.args.get("view", default="questions")  # questions | participants
    participant_id = request.args.get("participant_id", type=int)

    filters = {}

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, title FROM surveys ORDER BY created_at DESC")
            surveys = cur.fetchall()

            if not survey_id:
                return render_template(
                    "analytics.html",
                    surveys=surveys,
                    selected_survey=None,
                    survey_id=None,
                    overview=None,
                    qstats=[],
                    participants=[],
                    view=view,
                    participant_id=None,
                    selected_participant=None,
                    participant_detail=None,
                    participant_fields=[],
                    field_options={},
                    filters=filters,
                    qcharts_json="[]"
                )

            cur.execute("SELECT id, title, description FROM surveys WHERE id=%s", (survey_id,))
            selected_survey = cur.fetchone()
            if not selected_survey:
                return render_template(
                    "analytics.html",
                    surveys=surveys,
                    selected_survey=None,
                    survey_id=survey_id,
                    overview=None,
                    qstats=[],
                    participants=[],
                    view=view,
                    participant_id=None,
                    selected_participant=None,
                    participant_detail=None,
                    participant_fields=[],
                    field_options={},
                    filters=filters,
                    qcharts_json="[]"
                )

            participant_fields = []
            field_options = {}
            try:
                cur.execute("""
                    SELECT id, field_label, field_type
                    FROM participant_fields
                    WHERE survey_id=%s
                    ORDER BY id ASC
                """, (survey_id,))
                participant_fields = cur.fetchall()

                for f in participant_fields:
                    if f["field_type"] in ("single_choice", "multiple_choice"):
                        cur.execute("""
                            SELECT id, option_text
                            FROM participant_field_options
                            WHERE field_id=%s
                            ORDER BY id ASC
                        """, (f["id"],))
                        field_options[f["id"]] = cur.fetchall()
            except Exception:
                participant_fields = []
                field_options = {}

            cur.execute("SELECT COUNT(*) AS c FROM questions WHERE survey_id=%s", (survey_id,))
            total_questions = (cur.fetchone() or {}).get("c", 0) or 0

            cur.execute("SELECT COUNT(*) AS c FROM questions WHERE survey_id=%s AND is_required=1", (survey_id,))
            required_questions = (cur.fetchone() or {}).get("c", 0) or 0

            cur.execute("SELECT COUNT(*) AS c FROM participants WHERE survey_id=%s", (survey_id,))
            participant_count = (cur.fetchone() or {}).get("c", 0) or 0

            overview = {
                "participant_count": participant_count,
                "total_questions": total_questions,
                "required_questions": required_questions,
            }

            qstats = build_question_analytics(conn, survey_id, participant_count=participant_count)

            qcharts = []
            for q in qstats:
                qid = q.get("id")
                qtype = q.get("question_type")

                if qtype in ("single_choice", "multiple_choice"):
                    opts = q.get("options") or []
                    if opts:
                        qcharts.append({
                            "qid": qid,
                            "qtype": qtype,
                            "labels": [o.get("text") for o in opts],
                            "data": [int(o.get("count") or 0) for o in opts],
                        })

                elif qtype == "rating":
                    rating = q.get("rating") or {}
                    dist = rating.get("dist") or []
                    if dist:
                        qcharts.append({
                            "qid": qid,
                            "qtype": "rating",
                            "labels": [str(d.get("score")) for d in dist],
                            "data": [int(d.get("count") or 0) for d in dist],
                        })

            qcharts_json = json.dumps(qcharts, ensure_ascii=False)

            participants_sql = """
                SELECT p.id, p.first_name, p.last_name, p.email, p.created_at AS ts, p.duration_seconds
                FROM participants p
                WHERE p.survey_id=%s
            """
            params = [survey_id]

            # filtreler (pf_<id>)
            for f in participant_fields:
                key = f"pf_{f['id']}"
                val = (request.args.get(key) or "").strip()
                if not val:
                    continue

                if f["field_type"] == "text":
                    participants_sql += """
                        AND EXISTS (
                            SELECT 1
                            FROM participant_answers pa
                            WHERE pa.participant_id = p.id
                              AND pa.field_id = %s
                              AND pa.answer_text LIKE %s
                        )
                    """
                    params.extend([f["id"], f"%{val}%"])

                elif f["field_type"] in ("single_choice", "multiple_choice"):
                    participants_sql += """
                        AND EXISTS (
                            SELECT 1
                            FROM participant_answers pa
                            WHERE pa.participant_id = p.id
                              AND pa.field_id = %s
                              AND (
                                   pa.option_id = %s
                                   OR pa.answer_text = (SELECT option_text FROM participant_field_options WHERE id=%s)
                              )
                        )
                    """
                    try:
                        opt_id = int(val)
                    except:
                        opt_id = -1
                    params.extend([f["id"], opt_id, opt_id])

            participants_sql += " ORDER BY p.created_at DESC, p.id DESC"

            try:
                cur.execute(participants_sql, params)
                participants = cur.fetchall()
            except Exception as e:
                print("participants list error:", e)
                participants = []

            selected_participant = None
            participant_detail = None

            if participant_id:
                cur.execute("""
                    SELECT id, first_name, last_name, email, created_at AS ts, duration_seconds
                    FROM participants
                    WHERE survey_id=%s AND id=%s
                    LIMIT 1
                """, (survey_id, participant_id))
                selected_participant = cur.fetchone()

                if selected_participant:
                    cur.execute("""
                        SELECT id, question_text, question_type, is_required
                        FROM questions
                        WHERE survey_id=%s
                        ORDER BY id ASC
                    """, (survey_id,))
                    questions = cur.fetchall()

                    cur.execute("""
                        SELECT a.question_id,
                               a.option_id,
                               a.answer_text,
                               a.answer_number,
                               o.option_text
                        FROM answers a
                        JOIN responses r ON r.id = a.response_id
                        LEFT JOIN options o ON o.id = a.option_id
                        WHERE r.survey_id=%s AND r.participant_id=%s
                    """, (survey_id, participant_id))
                    rows = cur.fetchall()

                    by_q = {}
                    for r in rows:
                        qid = r.get("question_id")
                        by_q.setdefault(qid, []).append(r)

                    answered_count = 0
                    missing_required = 0
                    q_render = []

                    for q in questions:
                        qid = q["id"]
                        qtype = q["question_type"]
                        is_req = bool(q.get("is_required"))
                        ans_rows = by_q.get(qid, [])

                        has_answer = False
                        display_value = "-"

                        if qtype == "single_choice":
                            if ans_rows:
                                display_value = ans_rows[0].get("option_text") or "-"
                                has_answer = True

                        elif qtype == "multiple_choice":
                            opts = []
                            for r in ans_rows:
                                ot = (r.get("option_text") or "").strip()
                                if ot:
                                    opts.append(ot)
                            if opts:
                                display_value = ", ".join(opts)
                                has_answer = True

                        elif qtype == "rating":
                            if ans_rows:
                                r0 = ans_rows[0]
                                v = r0.get("answer_number")
                                if v is not None:
                                    display_value = str(v)
                                    has_answer = True
                                else:
                                    t = (r0.get("answer_text") or "").strip()
                                    if t:
                                        display_value = t
                                        has_answer = True
                                    else:
                                        ot = (r0.get("option_text") or "").strip()
                                        if ot:
                                            display_value = ot
                                            has_answer = True

                        elif qtype == "text":
                            if ans_rows:
                                t = (ans_rows[0].get("answer_text") or "").strip()
                                if t:
                                    display_value = t
                                    has_answer = True

                        if has_answer:
                            answered_count += 1
                        else:
                            if is_req:
                                missing_required += 1

                        q_render.append({
                            "id": qid,
                            "text": q["question_text"],
                            "type": qtype,
                            "is_required": is_req,
                            "answered": has_answer,
                            "value": display_value
                        })

                    participant_detail = {
                        "answered_count": answered_count,
                        "total_questions": len(questions),
                        "missing_required": missing_required,
                        "questions": q_render
                    }

            return render_template(
                "analytics.html",
                surveys=surveys,
                selected_survey=selected_survey,
                survey_id=survey_id,
                overview=overview,
                qstats=qstats,
                participants=participants,
                view=view,
                participant_id=participant_id,
                selected_participant=selected_participant,
                participant_detail=participant_detail,
                participant_fields=participant_fields,
                field_options=field_options,
                filters=filters,
                qcharts_json=qcharts_json
            )
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
