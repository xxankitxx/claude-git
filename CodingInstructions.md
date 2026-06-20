You are tasked with building a full‑stack paper trading system as per the specification below. 

The system must be written in Python with a Dash/React frontend. Use Breeze APIs (already integrated) for data. All logical components must be implemented as modular classes. 

Critical constraints:
1. Never miss a tick — use asyncio + multiprocessing.
2. OI-based support/resistance is the core.
3. Only trade Nifty, Bank Nifty, Fin Nifty options.
4. SL = -10 premium points; target min 6, ideal 12+, dynamic.
5. Capital resets to 100k daily.
6. No ordinary option chain display — only the panels described.

Provide complete code for each module: data ingestion, analytics, trade logic, UI, and audio. Add extensive comments. Include a requirements.txt.