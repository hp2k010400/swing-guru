"""Ordinary, realistic names for practice rivals, generated deterministically.

* About 85% traditionally male and 15% traditionally female first names, with British, Scottish, Irish,
  European and international names mixed together; no puns, animals or usernames.
* The n-th rival ever created always gets the same name (a fixed seed over fixed lists), and the name is
  stored with the rival, so it never changes across requests, restarts or weeks. Every name is unique, so a
  league can never show the same full name twice.
* Well-known professional golfers' names are excluded (``EXCLUDED``), so no rival impersonates a real player.
* Changing these lists changes future names only: existing rivals keep the name stored in the database.
"""

from __future__ import annotations

import random
from functools import lru_cache

SEED = "swingguru-rival-names-v1"
FEMALE_SHARE = 0.15

MALE = (
    "James", "Daniel", "Ryan", "Callum", "Michael", "Jack", "Ben", "Andrew", "Thomas", "Matthew", "David", "Chris",
    "Mark", "Paul", "Steven", "Scott", "Craig", "Gary", "Neil", "Stuart", "Iain", "Ross", "Euan", "Fraser", "Jamie",
    "Kieran", "Liam", "Conor", "Sean", "Declan", "Niall", "Ciaran", "Eoin", "Patrick", "Owen", "Rhys", "Gareth",
    "Dylan", "Aled", "Tom", "Joe", "Sam", "Harry", "George", "Oliver", "Will", "Alex", "Rob", "Nick", "Simon",
    "Richard", "Peter", "Martin", "Graham", "Alan", "Kevin", "Lewis", "Adam", "Luke", "Josh", "Nathan", "Kyle",
    "Aaron", "Jordan", "Marcus", "Lucas", "Mateo", "Luca", "Marco", "Pierre", "Henrik", "Lars", "Sven", "Jonas",
    "Tomasz", "Piotr", "Mikael", "Pablo", "Diego", "Carlos", "Raj", "Arjun", "Vikram", "Hamza", "Omar", "Yusuf",
    "Kenji", "Wei", "Kwame", "Tobi", "Mason", "Ethan", "Noah", "Elliot", "Finlay", "Hamish", "Angus", "Brendan",
    "Ronan", "Darragh", "Cillian", "Stephen", "Phil", "Colin", "Derek", "Duncan", "Gavin", "Jason", "Wayne",
)
FEMALE = (
    "Sophie", "Emma", "Olivia", "Hannah", "Lucy", "Katie", "Rachel", "Laura", "Amy", "Chloe", "Ellie", "Grace",
    "Isla", "Eilidh", "Niamh", "Aoife", "Siobhan", "Ciara", "Megan", "Rebecca", "Claire", "Jenny", "Fiona", "Kirsty",
    "Anna", "Lena", "Sofia", "Elena", "Marta", "Ingrid", "Priya", "Aisha", "Mei", "Yuki", "Zara", "Holly", "Abbie",
    "Jess", "Leah", "Freya",
)
SURNAMES = (
    "Wilson", "Fraser", "Mitchell", "Stewart", "Thompson", "Henderson", "Walker", "Campbell", "Anderson", "Robertson",
    "Martin", "Smith", "Brown", "Taylor", "Johnson", "Clarke", "Wright", "Hughes", "Edwards", "Green", "Hall",
    "Wood", "Harris", "Lewis", "Jackson", "Turner", "Hill", "Moore", "Cooper", "Ward", "Morris", "King", "Baker",
    "Parker", "Bennett", "Cook", "Bailey", "Bell", "Murphy", "Kelly", "Walsh", "Byrne", "Ryan", "O'Brien", "Doyle",
    "McCarthy", "Gallagher", "Quinn", "Kennedy", "Brennan", "Duffy", "Nolan", "MacDonald", "Ross", "Reid", "Murray",
    "Paterson", "Ferguson", "Grant", "Hamilton", "Graham", "Kerr", "Sinclair", "Munro", "Gordon", "Cameron", "Forbes",
    "Docherty", "McLean", "Burns", "Watson", "Davies", "Evans", "Jones", "Williams", "Price", "Powell", "Owen",
    "Morgan", "Griffiths", "Lloyd", "Patel", "Shah", "Khan", "Singh", "Ahmed", "Chen", "Wong",
    "Nguyen", "Tanaka", "Kim", "Okafor", "Mensah", "Silva", "Costa", "Rossi", "Bianchi", "Moreau", "Dubois", "Müller",
    "Schmidt", "Weber", "Jansen", "de Vries", "Nilsson", "Johansson", "Larsen", "Hansen", "Nowak", "Kowalski",
    "Novak", "García", "Fernández", "López", "Romero", "Sullivan", "Fletcher", "Spencer", "Barker", "Holmes",
    "Chapman", "Marshall", "Lawson", "Hunter", "Gibson", "Russell", "Pearce", "Webb", "Carter", "Dixon", "Harvey",
    "Stevens", "Rowe", "Booth", "Kirk", "Lowe", "Dunn", "Rees", "Hart", "Shaw", "Ellis", "Gray",
    "Payne", "Mills", "Knight", "Simpson", "Hudson", "Black", "Boyd", "Hay", "Milne", "Bruce", "Wallace", "Young",
)
# Well-known professional golfers (men's and women's tours, past and present) whose names an ordinary
# combination could reproduce. Lower case, "first last".
EXCLUDED = frozenset(s.lower() for s in (
    "Justin Rose", "Paul Casey", "Luke Donald", "Danny Willett", "Matt Fitzpatrick", "Matthew Fitzpatrick",
    "Tommy Fleetwood", "Tyrrell Hatton", "Lee Westwood", "Ian Poulter", "Rory McIlroy", "Shane Lowry", "Graeme McDowell",
    "Darren Clarke", "Padraig Harrington", "Seamus Power", "Tom McKibbin", "Colin Montgomerie", "Sandy Lyle",
    "Nick Faldo", "Paul Lawrie", "Robert MacIntyre", "Bob MacIntyre", "Russell Knox", "Stephen Gallacher",
    "Richie Ramsay", "Marc Warren", "Scott Jamieson", "Connor Syme", "Calum Hill", "Ewen Ferguson", "Grant Forrest",
    "David Law", "Martin Laird", "Jordan Smith", "Matt Wallace", "Matthew Wallace", "Aaron Rai", "Tom Lewis",
    "Chris Wood", "Oliver Wilson", "Ross Fisher", "Richard Bland", "Paul Waring", "Laurie Canter", "Andy Sullivan",
    "Eddie Pepperell", "Sam Horsfield", "Jamie Donaldson", "David Howell", "Daniel Brown", "Matthew Jordan",
    "Callum Shinkwin", "Jamie Spence", "Rhys Davies", "Bradley Dredge", "Phillip Price", "Ian Woosnam",
    "Simon Dyson", "Paul McGinley", "Michael Hoey", "Gareth Maybin", "Peter Lawrie", "David Lynn", "Barry Lane",
    "Mark James", "Howard Clark", "Sam Torrance", "Gordon Brand", "Ken Brown", "Peter Oosterhuis", "Tony Jacklin",
    "Brian Barnes", "Andrew Johnston", "Steven Brown", "Jason Day", "Adam Scott", "Cameron Smith", "Marc Leishman",
    "Scott Hend", "Lucas Herbert", "Jon Rahm", "Sergio Garcia", "Pablo Larrazabal", "Adri Arnaus", "Viktor Hovland",
    "Ludvig Aberg", "Henrik Stenson", "Alex Noren", "Thomas Pieters", "Thomas Detry", "Nicolai Hojgaard",
    "Rasmus Hojgaard", "Thorbjorn Olesen", "Martin Kaymer", "Bernhard Langer", "Francesco Molinari", "Guido Migliozzi",
    "Tiger Woods", "Jordan Spieth", "Justin Thomas", "Scottie Scheffler", "Xander Schauffele", "Collin Morikawa",
    "Brooks Koepka", "Dustin Johnson", "Phil Mickelson", "Patrick Reed", "Patrick Cantlay", "Bryson DeChambeau",
    "Tony Finau", "Max Homa", "Rickie Fowler", "Kevin Kisner", "Kevin Na", "Brian Harman", "Wyndham Clark",
    "Sam Burns", "Sungjae Im", "Tom Kim", "Hideki Matsuyama", "Ernie Els", "Gary Player", "Louis Oosthuizen",
    "Charl Schwartzel", "Dean Burmester", "Christiaan Bezuidenhout", "Georgia Hall", "Charley Hull", "Laura Davies",
    "Bronte Law", "Mel Reid", "Jodi Ewart Shadoff", "Leona Maguire", "Stephanie Meadow", "Catriona Matthew",
    "Gemma Dryburgh", "Kelly Tan", "Nelly Korda", "Jessica Korda", "Lexi Thompson", "Lydia Ko", "Rose Zhang",
    "Lilia Vu", "Minjee Lee", "Hannah Green", "Celine Boutier", "Anna Nordqvist", "Madelene Sagstrom",
    "Sophia Popov", "Esther Henseleit", "Annika Sorenstam", "Amy Yang", "Amy Olson", "Ally Ewing", "Nasa Hataoka",
    "Ayaka Furue", "Yuka Saso", "Brooke Henderson", "Lucy Li", "Emma Talley", "Ellie Spencer",
))


@lru_cache(maxsize=1)
def _sequence(length: int = 5000) -> tuple[str, ...]:
    rnd = random.Random(SEED)
    male = [f"{f} {s}" for f in MALE for s in SURNAMES if f"{f} {s}".lower() not in EXCLUDED and f != s]
    female = [f"{f} {s}" for f in FEMALE for s in SURNAMES if f"{f} {s}".lower() not in EXCLUDED and f != s]
    rnd.shuffle(male)
    rnd.shuffle(female)
    out: list[str] = []
    while len(out) < length and (male or female):
        pool = female if (female and (rnd.random() < FEMALE_SHARE or not male)) else male
        out.append(pool.pop())
    return tuple(out)


def name_for(index: int) -> str:
    """The name of the n-th practice rival ever created (stable and unique)."""

    seq = _sequence()
    if index < len(seq):
        return seq[index]
    return f"{seq[index % len(seq)]} {index // len(seq) + 1}"       # far beyond any realistic roster


@lru_cache(maxsize=1)
def _generated() -> frozenset[str]:
    return frozenset(n.lower() for n in _sequence())


def is_generated(name: str) -> bool:
    """Whether a name is one the generator produces (players choose league names only from these)."""

    return " ".join((name or "").split()).lower() in _generated()


def suggestions(user_id: str, page: int, taken: set[str], count: int = 4) -> list[str]:
    """``count`` generated names for a player to choose from (stable per player and page, never taken)."""

    seq = _sequence()
    rnd = random.Random(f"league-name-suggestions:{user_id}:{page}")
    lowered = {t.lower() for t in taken}
    out: list[str] = []
    for _ in range(400):
        name = seq[rnd.randrange(len(seq))]
        if name.lower() not in lowered and name not in out:
            out.append(name)
            if len(out) == count:
                break
    return out


def initials(name: str) -> str:
    parts = [p for p in name.replace("-", " ").split() if p[:1].isalpha()]
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper() if parts else "?"


def is_female(name: str) -> bool:
    return name.split()[0] in FEMALE


__all__ = ["EXCLUDED", "FEMALE_SHARE", "initials", "is_female", "is_generated", "name_for", "suggestions"]
