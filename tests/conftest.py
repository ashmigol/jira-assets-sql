"""In-memory fake of the Jira Assets API (same interface as assets_sql.api.Client)."""
import itertools
import re

import pytest

from assets_sql import store
from assets_sql.api import ApiError
from assets_sql.config import Config

SCHEMA = "5"
USERS = {"acc-alice": ("Alice Smith", "alice@example.com"), "acc-bob": ("Bob Jones", "bob@example.com")}


def attr(id, name, kind="Text", **kw):
    a = {"id": id, "name": name, "type": 0, "defaultType": {"name": kind}, "editable": True, "system": False,
         "maximumCardinality": 1, "minimumCardinality": 0, "label": False}
    if kind == "Reference":
        a.update(type=1, defaultType=None)
    if kind == "User":
        a.update(type=2, defaultType=None)
    a.update(kw)
    return a


def system_attrs(base):
    return [attr(base, "Key", editable=False, system=True), attr(base + 1, "Created", "DateTime", editable=False, system=True),
            attr(base + 2, "Updated", "DateTime", editable=False, system=True)]


TYPES = {
    10: ("Systems", system_attrs(100) + [
        attr(103, "Name", label=True, minimumCardinality=1),
        attr(104, "Criticality", "Select", options="Low,High"),
        attr(105, "Owner", "User", maximumCardinality=-1),
        attr(106, "Active", "Boolean"),
        attr(107, "Labels", maximumCardinality=-1),
    ]),
    11: ("Roles", system_attrs(110) + [
        attr(113, "Name", label=True, minimumCardinality=1),
        attr(114, "System", "Reference", referenceObjectTypeId=10),
        attr(115, "Approver", "User"),
        attr(116, "Google group"),
    ]),
}


class FakeJira:
    def __init__(self):
        self.objects = {}  # id → {"type": tid, "key": str, "values": {attr_id: [raw]}}
        self._ids = itertools.count(1000)
        self.calls = []
        self.fail = {}  # (method, id-or-name) → exception to raise once
        self.clock = itertools.count(1)

    # ── seeding / inspection ──
    def add(self, type_id, **values):
        """values by attribute name; refs as key, users as accountId, multi as list."""
        oid = str(next(self._ids))
        names = {a["name"]: a["id"] for a in TYPES[type_id][1]}
        vals = {names[k]: (v if isinstance(v, list) else [v]) for k, v in values.items() if v is not None}
        self.objects[oid] = {"type": type_id, "key": f"TST-{oid}", "values": vals, "updated": f"t{next(self.clock)}"}
        return self.objects[oid]["key"]

    def by_key(self, key):
        return next(o for o in self.objects.values() if o["key"] == key)

    def value(self, key, name):
        o = self.by_key(key)
        aid = next(a["id"] for a in TYPES[o["type"]][1] if a["name"] == name)
        return o["values"].get(aid)

    def set(self, key, name, raw):
        o = self.by_key(key)
        aid = next(a["id"] for a in TYPES[o["type"]][1] if a["name"] == name)
        o["values"][aid] = raw if isinstance(raw, list) else [raw]

    def of_type(self, tid):
        return [o for o in self.objects.values() if o["type"] == tid]

    # ── API shape ──
    def _label(self, o):
        aid = next(a["id"] for a in TYPES[o["type"]][1] if a["label"])
        return (o["values"].get(aid) or [""])[0]

    def _api(self, oid):
        o = self.objects[oid]
        attrs = []
        for a in TYPES[o["type"]][1]:
            if a["name"] == "Key":
                vals = [{"value": o["key"], "displayValue": o["key"]}]
            elif a["name"] in ("Created", "Updated"):
                vals = [{"value": o["updated"], "displayValue": o["updated"]}]
            elif a["type"] == 1:
                vals = []
                for k in o["values"].get(a["id"], []):
                    ref = self.by_key(k)
                    vals.append({"displayValue": self._label(ref), "searchValue": k})
            elif a["type"] == 2:
                vals = [{"value": u, "displayValue": f"{USERS[u][0]} ({USERS[u][1]})"} for u in o["values"].get(a["id"], [])]
            else:
                vals = [{"value": v, "displayValue": v} for v in o["values"].get(a["id"], [])]
            if vals:
                attrs.append({"objectTypeAttributeId": str(a["id"]), "objectAttributeValues": vals})
        return {"id": oid, "objectKey": o["key"], "label": self._label(o), "updated": o["updated"],
                "objectType": {"id": str(o["type"]), "name": TYPES[o["type"]][0]}, "attributes": attrs}

    def _maybe_fail(self, method, ident):
        exc = self.fail.pop((method, ident), None)
        if exc:
            raise exc

    def _write(self, oid, attrs):
        o = self.objects[oid]
        known = {a["id"]: a for a in TYPES[o["type"]][1]}
        for a in attrs:
            spec = known[a["objectTypeAttributeId"]]
            vals = [v["value"] for v in a["objectAttributeValues"]]
            for v in vals:
                if spec["type"] == 1 and not any(x["key"] == v for x in self.objects.values()):
                    raise ApiError(400, {"errors": f"unknown reference {v}"})
                if spec["type"] == 2 and v not in USERS:
                    raise ApiError(400, {"errors": f"unknown user {v}"})
            o["values"][spec["id"]] = vals
        o["updated"] = f"t{next(self.clock)}"

    # ── Client interface ──
    def object_types(self, schema):
        return [{"id": str(tid), "name": name} for tid, (name, _) in TYPES.items()]

    def attributes(self, type_id):
        return [dict(a) for a in TYPES[int(type_id)][1]]

    def aql(self, query, start=0, limit=100):
        self.calls.append(("AQL", query))
        m = re.fullmatch(r'objectTypeId = (\d+)(?: AND "([^"]+)" = "((?:[^"\\]|\\.)*)")?', query)
        assert m, f"unsupported AQL in fake: {query}"
        hits = [oid for oid, o in self.objects.items() if o["type"] == int(m.group(1))]
        if m.group(2):
            want = m.group(3).replace('\\"', '"').lower()
            hits = [oid for oid in hits if self._label(self.objects[oid]).lower() == want]
        page = hits[start:start + limit]
        return {"values": [self._api(oid) for oid in page], "isLast": start + limit >= len(hits), "total": len(hits)}

    def aql_all(self, query):
        out, start = [], 0
        while True:
            d = self.aql(query, start, 2)  # tiny pages to exercise pagination
            out += d["values"]
            start += len(d["values"])
            if d["isLast"]:
                return out

    def get_object(self, oid):
        self.calls.append(("GET", oid))
        self._maybe_fail("GET", oid)
        return self._api(oid) if oid in self.objects else None

    def create_object(self, type_id, attrs):
        self.calls.append(("POST", type_id, attrs))
        oid = str(next(self._ids))
        self.objects[oid] = {"type": int(type_id), "key": f"TST-{oid}", "values": {}, "updated": "t0"}
        try:
            names = {a["id"]: a["name"] for a in TYPES[int(type_id)][1]}
            label = next((v["objectAttributeValues"][0]["value"] for v in attrs if names[v["objectTypeAttributeId"]] == "Name"), None)
            self._maybe_fail("POST", label)
            self._write(oid, attrs)
        except Exception:
            del self.objects[oid]
            raise
        return {"id": oid, "objectKey": f"TST-{oid}"}

    def update_object(self, oid, type_id, attrs):
        self.calls.append(("PUT", oid, attrs))
        self._maybe_fail("PUT", oid)
        if oid not in self.objects:
            raise ApiError(404, "not found")
        self._write(oid, attrs)
        return self._api(oid)

    def delete_object(self, oid):
        self.calls.append(("DELETE", oid))
        self._maybe_fail("DELETE", oid)
        if self.objects.pop(oid, None) is None:
            raise ApiError(404, "not found")

    def account_id(self, email):
        match = [u for u, (_, e) in USERS.items() if e.lower() == email.lower()]
        if len(match) != 1:
            raise ValueError(f"user '{email}': not found in Jira")
        return match[0]

    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PUT", "DELETE")]


@pytest.fixture
def jira():
    j = FakeJira()
    pd = j.add(10, Name="Pandadoc", Criticality="High", Owner=["acc-alice"], Active="true")
    lk = j.add(10, Name="Lokalise", Criticality="Low", Active="false")
    j.add(11, Name="Admin", System=pd, Approver="acc-alice", **{"Google group": "pd-admins@example.com"})
    j.add(11, Name="Viewer", System=pd)
    j.add(11, Name="Admin", System=lk)  # same name as Pandadoc's Admin on purpose
    return j


@pytest.fixture
def cfg(tmp_path):
    return Config(site="https://example.atlassian.net", email="me@example.com", token="x", workspace_id="ws",
                  schema=SCHEMA, writable_schemas=frozenset({SCHEMA}), home=str(tmp_path))


@pytest.fixture
def local(cfg, jira):
    db, meta = store.open_db(cfg, jira, SCHEMA, log=lambda *a: None)
    return db, meta


def sql(db, meta, statement, writable=True):
    """Run a statement the way the shell does (under the guard)."""
    db.set_authorizer(store.guard(meta, writable))
    try:
        cur = db.execute(statement)
        db.commit()
        return cur
    finally:
        store.unguard(db)
