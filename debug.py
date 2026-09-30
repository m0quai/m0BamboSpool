# Local debug entry point for the m0BamboSpool custom application.
from app_custom import app

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
