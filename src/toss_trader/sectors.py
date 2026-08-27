from __future__ import annotations

from collections.abc import Mapping

# Standard Korean Industry Classification (KSIC) -> Sector Name
KSIC_PREFIX_TO_SECTOR: Mapping[str, str] = {
    "10": "음식료품",
    "11": "음식료품",
    "12": "음식료품",
    "13": "섬유의복",
    "14": "섬유의복",
    "15": "섬유의복",
    "16": "종이목재",
    "17": "종이목재",
    "18": "종이목재",
    "19": "화학",
    "20": "화학",
    "22": "화학",
    "21": "의약품",
    "23": "비금속광물",
    "24": "철강금속",
    "25": "철강금속",
    "26": "전기전자",
    "28": "전기전자",
    "27": "의료정밀",
    "29": "기계",
    "30": "운수장비",
    "31": "운수장비",
    "32": "기타제조",
    "33": "기타제조",
    "35": "전기가스",
    "41": "건설업",
    "42": "건설업",
    "45": "유통업",
    "46": "유통업",
    "47": "유통업",
    "49": "운수창고",
    "50": "운수창고",
    "51": "운수창고",
    "52": "운수창고",
    "58": "서비스업",
    "59": "서비스업",
    "60": "서비스업",
    "62": "서비스업",
    "63": "서비스업",
    "70": "서비스업",
    "71": "서비스업",
    "72": "서비스업",
    "73": "서비스업",
    "74": "서비스업",
    "75": "서비스업",
    "61": "통신업",
    "64": "금융업",
    "65": "금융업",
    "66": "금융업",
}


def ksic_to_sector(induty_code: str | None) -> str:
    if not induty_code:
        return "UNKNOWN"
    code = str(induty_code).strip()
    if len(code) >= 2:
        prefix = code[:2]
        if prefix in KSIC_PREFIX_TO_SECTOR:
            return KSIC_PREFIX_TO_SECTOR[prefix]
    return "UNKNOWN"
