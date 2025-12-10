from flask import Flask, render_template, request, redirect, url_for
import os
import pymysql

app = Flask(__name__)

# ---- VERİTABANI BAĞLANTISI ----
def get_db():
    conn = pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "mysql"),
        user=os.environ.get("MYSQL_USER", "root"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DB", "survey_app"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor
    )
    return conn

# ---- ANASAYFA -> ANKET LİSTESİNE YÖNLENDİR ----
@app.route("/")
def home():
    return redirect(url_for("list_surveys"))

# ---- ANKET LİSTESİ ----
@app.route("/surveys")
def list_surveys():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM surveys ORDER BY created_at DESC")
        surveys = cur.fetchall()
    conn.close()
    return render_template("survey_list.html", surveys=surveys)

# ---- YENİ ANKET OLUŞTUR ----
@app.route("/surveys/new", methods=["GET", "POST"])
def create_survey():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()

        if title:
            conn = get_db()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO surveys (title, description) VALUES (%s, %s)",
                    (title, description)
                )
                conn.commit()
            conn.close()
            return redirect(url_for("list_surveys"))

    return render_template("create_survey.html") 


@app.route("/surveys/<int:survey_id>/edit", methods=["GET", "POST"])
def edit_survey(survey_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM surveys WHERE id = %s", (survey_id,))
        survey = cur.fetchone()

    if not survey:
        conn.close()
        return "Anket bulunamadı", 404

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()

        if title:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE surveys SET title = %s, description = %s WHERE id = %s",
                    (title, description, survey_id)
                )
                conn.commit()
            conn.close()
            return redirect(url_for("list_surveys"))

    conn.close()
    return render_template("edit_survey.html", survey=survey)


# ---- SORU & ŞIK YÖNETİMİ ----
@app.route("/surveys/<int:survey_id>/questions", methods=["GET", "POST"])
def manage_questions(survey_id):
    conn = get_db()
    # Seçili anketi çek
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM surveys WHERE id = %s", (survey_id,))
        survey = cur.fetchone()
        if not survey:
            conn.close()
            return "Anket bulunamadı", 404

    # POST ise yeni soru/şık ekle
    if request.method == "POST":
        question_text = request.form.get("question_text", "").strip()
        option1 = request.form.get("option1", "").strip()
        option2 = request.form.get("option2", "").strip()
        option3 = request.form.get("option3", "").strip()
        option4 = request.form.get("option4", "").strip()

        if question_text:
            with conn.cursor() as cur:
                # Soruyu ekle
                cur.execute(
                    "INSERT INTO questions (survey_id, question_text) VALUES (%s, %s)",
                    (survey_id, question_text)
                )
                question_id = cur.lastrowid

                # Boş olmayan şıkları ekle
                for opt in [option1, option2, option3, option4]:
                    if opt:
                        cur.execute(
                            "INSERT INTO options (question_id, option_text) VALUES (%s, %s)",
                            (question_id, opt)
                        )
                conn.commit()

    # Tüm soruları ve şıkları listele
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM questions WHERE survey_id = %s", (survey_id,))
        questions = cur.fetchall()

        # her soruya şıklarını ekle
        for q in questions:
            cur.execute("SELECT * FROM options WHERE question_id = %s", (q["id"],))
            q["options"] = cur.fetchall()

    conn.close()
    return render_template("manage_questions.html", survey=survey, questions=questions)


@app.route("/surveys/<int:survey_id>/questions/<int:question_id>/delete", methods=["POST"])
def delete_question(survey_id, question_id):
    conn = get_db()
    with conn.cursor() as cur:
        # Bu soruya bağlı cevapları ve şıkları da sil
        cur.execute("DELETE FROM answers WHERE question_id = %s", (question_id,))
        cur.execute("DELETE FROM options WHERE question_id = %s", (question_id,))
        cur.execute("DELETE FROM questions WHERE id = %s AND survey_id = %s",
                    (question_id, survey_id))
        conn.commit()
    conn.close()
    return redirect(url_for("manage_questions", survey_id=survey_id))



@app.route("/surveys/<int:survey_id>/delete", methods=["POST"])
def delete_survey(survey_id):
    conn = get_db()
    with conn.cursor() as cur:
        # 1) Bu ankete ait tüm soru id'lerini al
        cur.execute("SELECT id FROM questions WHERE survey_id = %s", (survey_id,))
        rows = cur.fetchall()
        question_ids = [row["id"] for row in rows]

        if question_ids:
            # IN (%s, %s, ...) kısmını dinamik oluştur
            placeholders = ",".join(["%s"] * len(question_ids))

            # 2) Bu sorulara ait cevapları sil
            cur.execute(
                f"DELETE FROM answers WHERE question_id IN ({placeholders})",
                question_ids,
            )

            # 3) Bu sorulara ait şıkları sil
            cur.execute(
                f"DELETE FROM options WHERE question_id IN ({placeholders})",
                question_ids,
            )

            # 4) Soruları sil
            cur.execute(
                "DELETE FROM questions WHERE survey_id = %s",
                (survey_id,),
            )

        # 5) Bu ankete ait response kayıtlarını sil
        cur.execute(
            "DELETE FROM responses WHERE survey_id = %s",
            (survey_id,),
        )

        # 6) Son olarak anketin kendisini sil
        cur.execute(
            "DELETE FROM surveys WHERE id = %s",
            (survey_id,),
        )

        conn.commit()

    conn.close()
    return redirect(url_for("list_surveys"))



# ---- ANKETİ DOLDUR ----
@app.route("/surveys/<int:survey_id>/take", methods=["GET", "POST"])
def take_survey(survey_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM surveys WHERE id = %s", (survey_id,))
        survey = cur.fetchone()
        if not survey:
            conn.close()
            return "Anket bulunamadı", 404

        # Tüm soruları + şıklarını çek
        cur.execute("SELECT * FROM questions WHERE survey_id = %s", (survey_id,))
        questions = cur.fetchall()
        for q in questions:
            cur.execute("SELECT * FROM options WHERE question_id = %s", (q["id"],))
            q["options"] = cur.fetchall()

    if request.method == "POST":
        # Yeni bir response kaydı oluştur
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO responses (survey_id) VALUES (%s)", (survey_id,)
            )
            response_id = cur.lastrowid

            # Her soru için seçilen şıkkı al
            for q in questions:
                selected_option_id = request.form.get(f"question_{q['id']}")
                if selected_option_id:
                    cur.execute(
                        "INSERT INTO answers (response_id, question_id, option_id) "
                        "VALUES (%s, %s, %s)",
                        (response_id, q["id"], int(selected_option_id))
                    )
            conn.commit()

        conn.close()
        return redirect(url_for("show_results", survey_id=survey_id))

    conn.close()
    return render_template("take_survey.html", survey=survey, questions=questions)

# ---- SONUÇLAR ----
@app.route("/surveys/<int:survey_id>/results")
def show_results(survey_id):
    conn = get_db()
    with conn.cursor() as cur:
        # Anket bilgisi
        cur.execute("SELECT * FROM surveys WHERE id = %s", (survey_id,))
        survey = cur.fetchone()
        if not survey:
            conn.close()
            return "Anket bulunamadı", 404

        # Sorular
        cur.execute("SELECT * FROM questions WHERE survey_id = %s", (survey_id,))
        questions = cur.fetchall()

        # Her soru için şıklara göre sayım yap
        for q in questions:
            cur.execute(
                """
                SELECT o.id, o.option_text,
                       COUNT(a.id) AS vote_count
                FROM options o
                LEFT JOIN answers a ON a.option_id = o.id
                WHERE o.question_id = %s
                GROUP BY o.id, o.option_text
                """,
                (q["id"],)
            )
            options = cur.fetchall()

            # GERÇEK toplam oy
            total_votes = sum(o["vote_count"] for o in options)

            # Yüzde hesaplamak için bölende 0 olmasın diye fallback
            divisor = total_votes or 1

            for o in options:
                o["percent"] = round(o["vote_count"] * 100 / divisor, 1)

            q["options"] = options
            q["total_votes"] = total_votes   # <-- artık 0 da olabiliyor

    conn.close()
    return render_template("results.html", survey=survey, questions=questions)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
