"""
MYTHOS — Breeze credentials.

DAILY ROUTINE (before 09:15 IST):
  1. Open  https://api.icicidirect.com/apiuser/login?api_key=<URL-ENCODED-KEY>
  2. Log in -> the redirect URL contains apisession=XXXXXXXX
  3. Paste that number into SESSION_KEY below and save.

api_key / api_secret are long-lived; only SESSION_KEY changes daily.
Values pre-filled from the existing run7/login.py on this machine.
"""

API_KEY     = "z34Ys256N1o50713`y675j0566W)7L36"
API_SECRET  = "45P913Z54p9419@O701I83Z96)*5T50="
SESSION_KEY = "56020535"
