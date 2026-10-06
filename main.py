import uvicorn

from ledger.api import create_app

app = create_app(db_path="ledger.db")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
