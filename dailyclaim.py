from __future__ import annotations
import asyncio

import json
import os
from io import StringIO
from enum import Enum, auto
from datetime import datetime, timedelta
from typing import (
    AsyncIterator,
    Dict,
    List,
    NamedTuple,
    Optional,
    Tuple,
    TypedDict,
)
from dataclasses import dataclass

import discord
import httpx
import telegram
from bs4 import BeautifulSoup, NavigableString, Tag

from notif_discord import DiscordNotifier
from notif_tele import TeleNotifier

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


INCOME_URL = "https://kageherostudio.com/event/?event=daily"
XSS_LOGIN = "https://kageherostudio.com/payment/server_.php?fbid={}&selserver=1"
LOGIN_URL = "https://kageherostudio.com/event/index_.php?act=login"

DATE = datetime.utcnow() + timedelta(hours=7)  # GMT + 7 datetime
PERIOD = DATE.month
DCTOKEN = os.getenv("DISCORDTOKEN")
TELETOKEN = os.getenv("TELETOKEN")
TIMEOUT = httpx.Timeout(60 * 5)
FAIL_STATE: Dict[str, Exception] = {}


class ClaimStatus(Enum):
    SUCCESS = auto()
    FAILED = auto()
    CLAIMED = auto()


MSGSMAP = {
    ClaimStatus.SUCCESS: "**Succes** ✅",
    ClaimStatus.FAILED: "**Unclaimed** ❌",
    ClaimStatus.CLAIMED: "**Claimed** ✔️",
}


class UserData(TypedDict):
    email: str
    password: str
    server: int
    discord_id: int
    tele_id: int


class User(NamedTuple):
    user: str
    password: str


class UserStatus(NamedTuple):
    email: str
    statuses: List[ClaimData]
    last_claim: int
    discord: int
    tele: int

    @property
    def print_mail(self):
        email, mail = self.email.split("@", 1)
        head, tail = (email[:2], email[2:])
        return head + "+" * len(tail) + mail

    def print_status(self):
        info = StringIO()
        info.writelines(
            (
                f"Income report for: **{self.print_mail}**\n",
                f"**{DATE.date()}**\n",
                f"Last Claim: {self.last_claim if self.last_claim > 0 else 'No last claim!'}\n",
                # "\n".join(s.to_string() for s in self.statuses),
            )
        )
        for data in self.statuses:
            info.write(f"\n{data.to_string()}")
        return info.getvalue()


class ClaimCheck(Enum):
    CLAIMED = "grayscale"
    UNCLAIMED = "dailyClaim"
    CURRENT = "reward-star"

    def __str__(self) -> str:
        return self.value


@dataclass
class ClaimData:
    status: ClaimStatus
    day: int
    item: int
    name: str

    def to_string(self):
        return f"Item: {self.item}/Day {self.day} ({self.name}): \
            {MSGSMAP.get(self.status, 'Unclaimed!')}"


class AsyncItemIterator(AsyncIterator[Tuple[int, Tag | NavigableString]]):
    def __init__(self, soup: BeautifulSoup) -> None:
        self.tag = soup.find("div", str(ClaimCheck.UNCLAIMED))
        if not self.tag:
            raise StopAsyncIteration
        self.counter = 0

    def __aiter__(self) -> "AsyncItemIterator":
        return self

    async def __anext__(self):
        div = self.tag
        self.tag = self.tag.find_next_sibling("div", str(ClaimCheck.UNCLAIMED))
        if not self.tag:
            raise StopAsyncIteration
        self.counter += 1
        return self.counter, div


class DailyClaim:
    def __init__(self, email: str, server: int, passwd: Optional[str] = None) -> None:
        self.email = email
        self.passwd = passwd
        self.server = server
        self.baselogin = LOGIN_URL if passwd else XSS_LOGIN.format(email)
        self.cookies: Optional[httpx.Cookies] = None
        self.claim_data: List[ClaimData] = []

    async def reserve_cookie(self, client: httpx.AsyncClient):
        await client.get(INCOME_URL)
        if self.passwd:
            await client.post(
                self.baselogin,
                data={"txtuserid": self.email, "txtpassword": self.passwd},
            )
        else:
            await client.get(self.baselogin)
        self.cookies = client.cookies
        return await client.get(INCOME_URL)

    async def check_unclaimed(self):
        async with httpx.AsyncClient(timeout=TIMEOUT, cookies=self.cookies) as client:
            if not self.cookies:
                resp = await self.reserve_cookie(client)
            else:
                resp = await client.get(INCOME_URL)
        soup = BeautifulSoup(await resp.aread(), "html.parser")
        userid = soup.find("p", class_="userid")
        if not userid:
            print(f"Failed to login for {self.email}")
            return [], None
        claim_data: List[ClaimData] = []
        today_claim: Optional[ClaimData] = None
        async for idx, elem in AsyncItemIterator(soup):
            claimed = str(ClaimCheck.CLAIMED) in elem["class"]
            data = ClaimData(
                ClaimStatus.CLAIMED if claimed else ClaimStatus.FAILED,
                idx,
                int(elem["data-id"]),
                elem["data-name"],
            )
            claim_data.append(data)
            if str(ClaimCheck.CURRENT) in elem["class"]:
                today_claim = data
        return (claim_data, today_claim)

    async def perform_claim(self):
        print("Performing claim for", self.email)
        self.claim_data, today = await self.check_unclaimed()
        if today:
            async with httpx.AsyncClient(cookies=self.cookies) as client:
                if not self.cookies:
                    await self.reserve_cookie(client)
                result = await client.post(
                    "https://kageherostudio.com/event/index_.php?act=daily",
                    data={
                        "itemId": today.item,
                        "periodId": PERIOD,
                        "selserver": self.server,
                    },
                )
                resdata = result.json()

            if resdata["message"] == "success":
                today.status = ClaimStatus.SUCCESS
                return True
        return False

    def __repr__(self) -> str:
        return f"DailyClaim(user: {self.email}, \
            use_password: {bool(self.passwd)}, \
            server: {self.server})"


async def run_discord(statuses):
    dc_bot = DiscordNotifier(statuses)
    try:
        if DCTOKEN:
            await dc_bot.start(DCTOKEN)
        else:
            print("No discord token were provide")
    except discord.LoginFailure as exc:
        await dc_bot.close()
        FAIL_STATE.update({"discord": exc})


async def run_tele(statuses):
    try:
        if TELETOKEN:
            tele_bot = TeleNotifier(TELETOKEN, statuses)
            async with tele_bot.app:
                await tele_bot.app.start()
                await tele_bot.send_message()
                await tele_bot.app.stop()
        else:
            print("No tele token were provided")
    except telegram.error.InvalidToken as exc:
        FAIL_STATE.update({"telegram": exc})


async def main():
    with open("data.json", "r", encoding="utf-8") as file:
        data: List[UserData] = json.load(file)

    statuses: List[UserStatus] = []

    async with asyncio.TaskGroup() as tasks:
        for userdata in data:
            daily = DailyClaim(
                userdata["email"], userdata["server"], userdata["password"]
            )
            tasks.create_task(daily.perform_claim())
    print("Done running claim!")
    for userdata in data:
        if not daily.claim_data:
            print("NO DATA FOUND FOR:", userdata["email"])
            continue
        success = list(
            d
            for d in daily.claim_data
            if d.status in [ClaimStatus.CLAIMED, ClaimStatus.SUCCESS]
        )
        statuses.append(
            status := UserStatus(
                userdata["email"],
                daily.claim_data,
                max(d.day for d in success) if success else -1,
                userdata.get("discord_id", 0),
                userdata.get("tele_id", 0),
            )
        )
        print(status.print_status())
        print("=" * 20)
    await run_discord(statuses)
    await run_tele(statuses)
    if FAIL_STATE:
        key, exc = FAIL_STATE.popitem()
        raise RuntimeError(f"An improper {key} token was passed!") from exc


if __name__ == "__main__":
    asyncio.run(main())
