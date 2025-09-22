from typing import Any, Text, Dict, List, Optional
from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher
import logging
import re
from difflib import get_close_matches
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

logger = logging.getLogger(__name__)

SCHEDULES = {
    ("Kraków", "Łódź"): ["04:45", "08:30", "12:20", "16:00", "20:15"],
    ("Łódź", "Kraków"): ["05:10", "09:00", "13:45", "18:20"],
    ("Warszawa", "Poznań"): ["06:00", "07:30", "09:00", "11:45", "14:30"],
    ("Poznań", "Warszawa"): ["05:50", "10:10", "13:00", "17:25"],
    ("Warszawa", "Kraków"): ["05:30", "09:40", "15:10", "19:00"],
}

DELAYS_CITY = {
    "Kraków": "utrudnienia: w kierunku Warszawa; na odcinku Kraków - Wadowice występują objazdy.",
    "Łódź": "komunikacja zastępcza na odcinku Koluszki - Skierniewice (do odwołania).",
    "Warszawa": "utrudnienia w ruchu w stronę Radomia, możliwe krótkie opóźnienia.",
    "Poznań": "brak ogłoszonych utrudnień."
}

DELAYS_TRAIN = {
    "IC 1234": "opóźniony o 20 minut",
    "TLK 4567": "odwołany na odcinku Warszawa - Łódź",
    "EIP 123": "kursuje zgodnie z rozkładem",
}

TICKET_PRICES = {
    ("Łódź", "Kraków"): "50 zł (standardowy)",
    ("Warszawa", "Poznań"): "80 zł (standardowy)",
    ("Warszawa", "Kraków"): "90 zł (standardowy)",
}

PLATFORMS = {
    "IC 1234": "peron 5",
    "TLK 4567": "peron 2",
    "EIP 123": "peron 7",
}

TRAIN_TYPES = {
    ("Łódź", "Kraków"): "IC",
    ("Warszawa", "Poznań"): "EIP",
    ("Warszawa", "Kraków"): "EIP/IC (w zależności od kursu)",
}

TRAIN_SERVICES = {
    "IC 1234": ["Wi-Fi", "restauracja", "klimatyzacja", "wagon sypialny (na wybranych kursach)"],
    "TLK 4567": ["klimatyzacja"],
    "EIP 123": ["Wi-Fi", "restauracja", "przedziały 1 klasy"],
}

KNOWN_CITIES = sorted({c for pair in list(SCHEDULES.keys()) for c in pair} | set(DELAYS_CITY.keys()))

CITY_ALIASES = {
    "Krakowa": "Kraków",
    "Krakowie": "Kraków",
    "Kraków": "Kraków",
    "Krakow": "Kraków",
    "Łodzi": "Łódź",
    "Łódź": "Łódź",
    "Lodz": "Łódź",
    "Warszawy": "Warszawa",
    "Warszawie": "Warszawa",
    "Warszawa": "Warszawa",
    "Poznania": "Poznań",
    "Poznań": "Poznań",
}

def normalize_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"[^\w\s\-\u0100-\u017F]", "", text)
    return text

def normalize_city(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip()
    if raw in CITY_ALIASES:
        return CITY_ALIASES[raw]
    for k, v in CITY_ALIASES.items():
        if k.lower() == raw.lower():
            return v
    candidates = get_close_matches(raw, KNOWN_CITIES, n=1, cutoff=0.6)
    if candidates:
        return candidates[0]
    low = raw.lower().replace("ł", "l")
    for k in KNOWN_CITIES:
        if k.lower().replace("ł", "l") == low:
            return k
    return raw.title()

def normalize_train_number(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip().upper()
    m = re.match(r"([A-ZĄĆĘŁŃÓŚŻŹ]{1,4})\s*-?\s*(\d{1,5})", raw)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return raw

def now_warsaw():
    if ZoneInfo:
        tz = ZoneInfo("Europe/Warsaw")
        return datetime.now(tz)
    else:
        return datetime.now()

def time_str_to_minutes(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m

def minutes_to_time_str(m: int) -> str:
    h = (m // 60) % 24
    mm = m % 60
    return f"{h:02d}:{mm:02d}"

def find_next_train(trains: List[str]) -> Optional[str]:
    if not trains:
        return None
    now = now_warsaw()
    current_min = now.hour * 60 + now.minute
    mins = sorted([time_str_to_minutes(t) for t in trains])
    for m in mins:
        if m >= current_min:
            return minutes_to_time_str(m)
    return minutes_to_time_str(mins[0])

class ActionShowSchedule(Action):
    def name(self) -> Text:
        return "action_show_schedule"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        departure = tracker.get_slot("departure_city")
        arrival = tracker.get_slot("arrival_city")
        for ent in tracker.latest_message.get("entities", []) or []:
            if ent.get("entity") == "departure_city" and not departure:
                departure = ent.get("value")
            if ent.get("entity") == "arrival_city" and not arrival:
                arrival = ent.get("value")
            if ent.get("entity") == "from_city" and not departure:
                departure = ent.get("value")
            if ent.get("entity") == "to_city" and not arrival:
                arrival = ent.get("value")

        departure = normalize_city(normalize_text(departure)) if departure else None
        arrival = normalize_city(normalize_text(arrival)) if arrival else None

        if not departure or not arrival:
            dispatcher.utter_message(text="Podaj proszę miasto początkowe i docelowe (np. 'z Łodzi do Krakowa').")
            return []

        key = (departure, arrival)
        if key not in SCHEDULES:
            reverse = (arrival, departure)
            if reverse in SCHEDULES:
                dispatcher.utter_message(text=(f"Rozkład jest dostępny w odwrotną stronę ({arrival} → {departure}). Czy o to chodziło?"))
                return []
            alternatives = [k for k in SCHEDULES.keys() if k[0].lower() == departure.lower() or k[1].lower() == arrival.lower()]
            if alternatives:
                sample = alternatives[0]
                dispatcher.utter_message(text=(f"Nie mam rozkładu dla trasy {departure} → {arrival}, ale mam informacje dla trasy {sample[0]} → {sample[1]}: {', '.join(SCHEDULES[sample])}."))
            else:
                dispatcher.utter_message(text=f"Niestety nie mam rozkładu dla trasy {departure} → {arrival}.")
            return []

        trains = SCHEDULES[key]
        intent = tracker.latest_message.get("intent", {}).get("name")

        if intent in ("ask_schedule_next", "ask_schedule"):
            next_train = find_next_train(trains)
            dispatcher.utter_message(text=f"Najbliższy pociąg z {departure} do {arrival} odjeżdża o {next_train}.")
        elif intent == "ask_schedule_all" or intent == "ask_schedule_connection":
            dispatcher.utter_message(text=f"Wszystkie dostępne odjazdy z {departure} do {arrival}: {', '.join(trains)}.")
        else:
            next_train = find_next_train(trains)
            dispatcher.utter_message(text=(f"Połączenia z {departure} do {arrival}: {', '.join(trains)}. Najbliższy: {next_train}."))
        return []

class ActionShowDelay(Action):
    def name(self) -> Text:
        return "action_show_delay"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        intent = tracker.latest_message.get("intent", {}).get("name")
        delay_city = tracker.get_slot("delay_city")
        train_number = tracker.get_slot("train_number")
        for ent in tracker.latest_message.get("entities", []) or []:
            if ent.get("entity") == "delay_city" and not delay_city:
                delay_city = ent.get("value")
            if ent.get("entity") == "train_number" and not train_number:
                train_number = ent.get("value")

        delay_city = normalize_city(normalize_text(delay_city)) if delay_city else None
        train_number = normalize_train_number(train_number) if train_number else None

        if intent in ("ask_delay_city", "ask_delay"):
            if not delay_city:
                dispatcher.utter_message(text="Podaj proszę miasto, z którego chcesz sprawdzić opóźnienia (np. 'opóźnienia z Krakowa').")
                return []
            info = DELAYS_CITY.get(delay_city)
            if info:
                dispatcher.utter_message(text=f"Aktualne informacje dla {delay_city}: {info}.")
            else:
                dispatcher.utter_message(text=f"Brak informacji o opóźnieniach w {delay_city}.")
            return []
        elif intent == "ask_delay_train":
            if not train_number:
                dispatcher.utter_message(text="Podaj proszę numer pociągu (np. 'IC 1234').")
                return []
            info = DELAYS_TRAIN.get(train_number)
            if info:
                dispatcher.utter_message(text=f"Pociąg {train_number}: {info}.")
            else:
                dispatcher.utter_message(text=f"Brak informacji o opóźnieniach dla pociągu {train_number}.")
            return []
        else:
            if delay_city:
                info = DELAYS_CITY.get(delay_city)
                if info:
                    dispatcher.utter_message(text=f"Aktualnie dla {delay_city}: {info}.")
                    return []
            if train_number:
                info = DELAYS_TRAIN.get(train_number)
                if info:
                    dispatcher.utter_message(text=f"Pociąg {train_number}: {info}.")
                    return []
            dispatcher.utter_message(text="Nie rozumiem — podaj proszę miasto lub numer pociągu, którego dotyczy zapytanie o opóźnienia.")
            return []

class ActionCheckSchedule(Action):
    def name(self) -> Text:
        return "action_check_schedule"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        from_city = next(tracker.get_latest_entity_values("from_city"), None) or tracker.get_slot("departure_city")
        to_city = next(tracker.get_latest_entity_values("to_city"), None) or tracker.get_slot("arrival_city")

        if not from_city or not to_city:
            dispatcher.utter_message(text="Podaj proszę miasto początkowe i docelowe.")
            return []

        dispatcher.utter_message(text=f"Sprawdzam rozkład jazdy z {from_city} do {to_city}...")
        dispatcher.utter_message(text=f"Najbliższy pociąg z {from_city} do {to_city} odjeżdża o 12:45 🚆")
        return []

class ActionShowTicketPrice(Action):
    def name(self) -> Text:
        return "action_show_ticket_price"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        departure = tracker.get_slot("departure_city")
        arrival = tracker.get_slot("arrival_city")
        for ent in tracker.latest_message.get("entities", []) or []:
            if ent.get("entity") == "departure_city" and not departure:
                departure = ent.get("value")
            if ent.get("entity") == "arrival_city" and not arrival:
                arrival = ent.get("value")

        departure = normalize_city(normalize_text(departure)) if departure else None
        arrival = normalize_city(normalize_text(arrival)) if arrival else None

        if not departure or not arrival:
            dispatcher.utter_message(text="Podaj miasta początkowe i docelowe, np. 'ile kosztuje bilet z Łodzi do Krakowa'.")
            return []

        price = TICKET_PRICES.get((departure, arrival)) or TICKET_PRICES.get((arrival, departure))
        if price:
            dispatcher.utter_message(text=f"Cena biletu z {departure} do {arrival}: {price}.")
        else:
            dispatcher.utter_message(text=f"Brak danych o cenie biletu dla trasy {departure} → {arrival}. Możesz spróbować innych wariantów (np. różni przewoźnicy).")
        return []

class ActionShowPlatform(Action):
    def name(self) -> Text:
        return "action_show_platform"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        train_number = tracker.get_slot("train_number")
        for ent in tracker.latest_message.get("entities", []) or []:
            if ent.get("entity") == "train_number" and not train_number:
                train_number = ent.get("value")
        train_number = normalize_train_number(train_number) if train_number else None
        if not train_number:
            dispatcher.utter_message(text="Podaj numer pociągu, np. 'IC 1234'.")
            return []
        platform = PLATFORMS.get(train_number)
        if platform:
            dispatcher.utter_message(text=f"Pociąg {train_number} odjeżdża z {platform}.")
        else:
            dispatcher.utter_message(text=f"Brak danych o peronie dla pociągu {train_number}.")
        return []

class ActionShowTrainType(Action):
    def name(self) -> Text:
        return "action_show_train_type"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        departure = tracker.get_slot("departure_city")
        arrival = tracker.get_slot("arrival_city")
        for ent in tracker.latest_message.get("entities", []) or []:
            if ent.get("entity") == "departure_city" and not departure:
                departure = ent.get("value")
            if ent.get("entity") == "arrival_city" and not arrival:
                arrival = ent.get("value")
        departure = normalize_city(normalize_text(departure)) if departure else None
        arrival = normalize_city(normalize_text(arrival)) if arrival else None
        if not departure or not arrival:
            dispatcher.utter_message(text="Podaj miasta początkowe i docelowe, np. 'jaki typ pociągu z Łodzi do Krakowa'.")
            return []
        train_type = TRAIN_TYPES.get((departure, arrival))
        if train_type:
            dispatcher.utter_message(text=f"Na trasie {departure} → {arrival} kursuje pociąg typu: {train_type}.")
        else:
            dispatcher.utter_message(text=f"Brak danych o typie pociągu na trasie {departure} → {arrival}.")
        return []

class ActionShowServices(Action):
    def name(self) -> Text:
        return "action_show_services"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        train_number = tracker.get_slot("train_number")
        for ent in tracker.latest_message.get("entities", []) or []:
            if ent.get("entity") == "train_number" and not train_number:
                train_number = ent.get("value")
        train_number = normalize_train_number(train_number) if train_number else None
        if not train_number:
            dispatcher.utter_message(text="Podaj numer pociągu, np. 'IC 1234', abym mógł sprawdzić usługi.")
            return []
        services = TRAIN_SERVICES.get(train_number, [])
        if services:
            dispatcher.utter_message(text=f"Pociąg {train_number} oferuje: {', '.join(services)}.")
        else:
            dispatcher.utter_message(text=f"Brak informacji o usługach w pociągu {train_number}.")
        return []