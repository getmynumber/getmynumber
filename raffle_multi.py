from flask import Flask, render_template_string, request, redirect, url_for
import random

app = Flask(__name__)

# ---- Configuration ----
MAX_NUMBER = 500
used_numbers = set()

# ---- Home Page ----
@app.route("/", methods=["GET", "POST"])
def index():
    global used_numbers
    if request.method == "POST":
        available_numbers = [n for n in range(1, MAX_NUMBER + 1) if n not in used_numbers]
        if not available_numbers:
            return "All numbers taken!"
        chosen = random.choice(available_numbers)
        used_numbers.add(chosen)
        return redirect(url_for("result", number=chosen))
    return render_template_string("""
        <h1>Get My Number</h1>
        <form method="post">
            <label>Name: <input type="text" name="name" required></label><br>
            <label>Email: <input type="email" name="email" required></label><br>
            <label>Phone: <input type="text" name="phone"></label><br><br>
            <button type="submit">Choose my number</button>
        </form>
    """)

# ---- Result Page ----
@app.route("/result/<int:number>")
def result(number):
    donation_url = f"https://www.charityextra.com/charity/kehilla?amount={number}"
    return render_template_string(f"""
        <h2>Your number is {number}!</h2>
        <p>Click below to donate £{number}.</p>
        <a href="{donation_url}" target="_blank">
            <button>Donate £{number}</button>
        </a>
    """)

if __name__ == "__main__":
    app.run(debug=True)
