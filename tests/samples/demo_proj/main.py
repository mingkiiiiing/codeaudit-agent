"""入口模块：调用 services。"""

from app.services.orders import load_totals
from app.utils.net import fetch_json


def main():
    totals = load_totals(["a.txt", "b.txt"])
    print(totals)
    print(fetch_json("https://example.com/api"))


if __name__ == "__main__":
    main()
