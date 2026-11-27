"""AnnSeva recommendation engine with honest cold-start behavior and model caching."""
from __future__ import annotations
import sqlite3, hashlib, json
from pathlib import Path
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
import joblib

FEATURES=["crop_match","quantity_fit","location_match","verified","priority_score"]
ROOT=Path(__file__).resolve().parent.parent
DB_PATH=ROOT/"annseva.db"
MODEL_PATH=ROOT/"ml"/"random_forest.joblib"
META_PATH=ROOT/"ml"/"random_forest.meta.json"


def load_training_data():
    if not DB_PATH.exists(): return pd.DataFrame()
    with sqlite3.connect(DB_PATH) as con:
        return pd.read_sql_query("SELECT crop_match,quantity_fit,location_match,verified,priority_score,outcome FROM model_history WHERE outcome BETWEEN 0 AND 1 ORDER BY id",con)


def train_model():
    data=load_training_data()
    if len(data)<20 or data["outcome"].nunique()<2: return None
    signature=hashlib.sha256(data.to_csv(index=False).encode()).hexdigest()
    try:
        if MODEL_PATH.exists() and META_PATH.exists():
            meta=json.loads(META_PATH.read_text())
            if meta.get("signature")==signature: return joblib.load(MODEL_PATH)
    except Exception: pass
    model=RandomForestRegressor(n_estimators=250,max_depth=8,min_samples_leaf=2,random_state=42,n_jobs=-1)
    model.fit(data[FEATURES],data["outcome"])
    MODEL_PATH.parent.mkdir(parents=True,exist_ok=True)
    joblib.dump(model,MODEL_PATH)
    META_PATH.write_text(json.dumps({"signature":signature,"rows":len(data)}))
    return model


def quantity_fit(available,required):
    if required<=0:return 0.0
    return min(float(available),float(required))/float(required)


def baseline_score(features):
    return (0.30*features["crop_match"]+0.25*features["quantity_fit"]+0.20*features["location_match"]+0.15*features["verified"]+0.10*features["priority_score"])


def recommendation_score(model,farmer,requirement):
    features={"crop_match":int(farmer["crop"].strip().lower()==requirement["crop"].strip().lower()),
              "quantity_fit":quantity_fit(farmer["quantity"],requirement["remaining_quantity"]),
              "location_match":float(farmer["location_score"]),"verified":int(farmer["verified"]),"priority_score":float(farmer["priority_score"])}
    if model is None:return float(baseline_score(features)*100)
    return float(max(0,min(1,model.predict(pd.DataFrame([features])[FEATURES])[0]))*100)


def recommend_farmers(farmers,requirement,top_k=5,return_meta=False):
    model=train_model(); rows=[]
    for farmer in farmers:
        if farmer["crop"].strip().lower()!=requirement["crop"].strip().lower():continue
        if not farmer["verified"] or farmer["quantity"]<=0:continue
        rows.append({"farmer_id":farmer["farmer_id"],"name":farmer["name"],"crop":farmer["crop"],"available_quantity":farmer["quantity"],"location":farmer["location"],"match_score":round(recommendation_score(model,farmer,requirement),2)})
    df=pd.DataFrame(rows)
    if not df.empty: df=df.sort_values("match_score",ascending=False).head(top_k).reset_index(drop=True)
    if return_meta:return df,"random_forest" if model is not None else "cold_start_baseline"
    return df


def allocate_procurement(ranked_farmers,required_quantity,centre_capacity,already_allocated=0):
    if ranked_farmers.empty:return ranked_farmers.copy()
    demand=max(0,float(required_quantity)); capacity=max(0,float(centre_capacity)-float(already_allocated)); out=[]
    for _,farmer in ranked_farmers.iterrows():
        if demand<=0 or capacity<=0:break
        qty=min(float(farmer["available_quantity"]),demand,capacity)
        out.append({"farmer_id":farmer["farmer_id"],"name":farmer["name"],"match_score":farmer["match_score"],"recommended_quantity":round(qty,2),"centre":""})
        demand-=qty; capacity-=qty
    return pd.DataFrame(out)
