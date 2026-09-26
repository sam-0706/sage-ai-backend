import os

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
os.environ.setdefault("CLERK_SECRET_KEY", "sk_test_x")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ["APP_ENV"] = "test"
os.environ["DEV_AUTH_BYPASS"] = "false"
os.environ["RAZORPAY_KEY_ID"] = "rzp_test_abc"
os.environ["RAZORPAY_KEY_SECRET"] = "secret123"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "whsecret"
