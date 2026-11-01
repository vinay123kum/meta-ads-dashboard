"""Seed a demo DB with fictional accounts so the dashboard runs without a Meta token.
    AGENCY_DB=demo.db python seed_mock.py
Every brand, campaign and number here is made up."""
import random
from datetime import date, timedelta
import db

ANCHOR = date.today() - timedelta(days=1)
DAYS = 95
NOW = ANCHOR.isoformat() + "T00:00:00Z"
random.seed(21)


def dstr(i):
    return (ANCHOR - timedelta(days=i)).isoformat()


# id, name, daily spend, ctr %, conversions/day, cost per impression, value per conversion
ACCOUNTS = [
    ("act_101", "Northwind Fitness", 210, 2.8, 14, .016, 62),
    ("act_102", "Maple Street Coffee", 95, 3.6, 11, .011, 24),
    ("act_103", "Blue Harbor Dental", 160, 1.9, 5, .022, 180),
    ("act_104", "Fabrikam Home", 320, 1.4, 16, .019, 88),
    ("act_105", "Tailspin Travel", 275, 2.2, 7, .024, 210),
    ("act_106", "Lumen Skincare", 140, 3.1, 12, .014, 46),
    ("act_107", "Contoso Pet Supply", 80, 2.5, 0, .015, 0),
    ("act_108", "Adatum Learning", 0, 0, 0, .015, 0),
]
CAMPS = [("Prospecting - Broad", .5, 1.15, .45), ("Retargeting - 30 Day", .25, 1.5, .4),
         ("Offer - Seasonal", .17, .9, .13), ("Brand - Video", .08, .6, .02)]
ADS = ["Testimonial v3", "Founder Story", "Before and After", "Offer Static"]


def noise():
    return random.uniform(.86, 1.14)


def main():
    db.init()
    con = db.connect()
    for aid, name, spend, ctr, conv, cost, value in ACCOUNTS:
        db.upsert(con, "accounts", {"id": aid, "account_id": aid.split("_")[1], "name": name, "currency": "USD",
                                    "status": "ACTIVE", "updated_at": NOW})
        db.upsert(con, "account_budgets", {"account_id": aid, "monthly_budget": round(spend * 45 / 100) * 100, "updated_at": NOW})
        for i in range(DAYS):
            sp = spend * (1 + (DAYS - i) * .003) * noise()
            impr = int(sp / cost)
            cv = conv * (1 + (DAYS - i) * .003) * noise()
            row = {"spend": round(sp, 2), "impressions": impr, "clicks": int(impr * ctr / 100), "conversions": round(cv, 1)}
            db.upsert(con, "insights_daily", {"account_id": aid, "campaign_id": "ACCOUNT", "date": dstr(i), **row,
                                              "conv_value": round(cv * value, 2)})
            db.upsert(con, "geo_daily", {"account_id": aid, "date": dstr(i), "country": "US", **row})
        if not spend:
            continue
        for n, (cname, share, cm, cvs) in enumerate(CAMPS):
            cid = f"{aid}_c{n}"
            db.upsert(con, "campaigns", {"id": cid, "account_id": aid, "name": cname, "objective": "OUTCOME_LEADS",
                                         "status": "ACTIVE", "daily_budget": round(spend * share), "updated_at": NOW})
            for i in range(30):
                sp = spend * share * (1 + (30 - i) * .003) * noise()
                impr = int(sp / cost)
                cv = conv * cvs * noise()
                db.upsert(con, "insights_daily", {"account_id": aid, "campaign_id": cid, "date": dstr(i), "spend": round(sp, 2),
                                                  "impressions": impr, "clicks": int(impr * ctr * cm / 100),
                                                  "conversions": round(cv, 1), "conv_value": round(cv * value, 2)})
        for n, ad in enumerate(ADS):
            ad_id = f"{aid}_a{n}"
            db.upsert(con, "creatives", {"id": ad_id, "account_id": aid, "campaign_id": f"{aid}_c0", "name": ad,
                                         "is_video": int(n < 2), "updated_at": NOW})
            for i in range(30):
                age = 30 - i
                sp = spend * .12 * noise()
                impr = int(sp / cost)
                fade = 1 - (age / 60 if n == 0 else 0)
                clicks = int(impr * ctr * fade / 100)
                db.upsert(con, "creative_daily", {"ad_id": ad_id, "account_id": aid, "date": dstr(i), "spend": round(sp, 2),
                                                  "impressions": impr, "clicks": clicks, "link_clicks": int(clicks * .8),
                                                  "video_3s": int(impr * .3 * fade) if n < 2 else 0,
                                                  "thruplay": int(impr * .09 * fade) if n < 2 else 0,
                                                  "frequency": round(1.2 + age * (.09 if n == 0 else .02), 2),
                                                  "cpm": round(cost * 1000, 2), "ctr": round(ctr * fade, 2)})
        con.commit()
    con.commit()
    con.close()
    print("Seeded demo DB.")


if __name__ == "__main__":
    main()
