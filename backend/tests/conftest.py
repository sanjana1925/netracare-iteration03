import csv
import os
import shutil

import pytest
from fastapi.testclient import TestClient

from helpers import CLINIC_NOW, DATA


@pytest.fixture(scope="session", autouse=True)
def real_model_required():
    """These tests call the REAL language model (there is no stand-in model anywhere). Stop at once, with a clear
    message, if no key is configured, instead of letting every booking fail with a confusing 503."""
    from app import llm
    if not llm.llm_configured():
        pytest.exit("No LLM key found. These tests call the real model: put your key in backend/.env (see .env.example).", returncode=2)


def _build_template(tmp_path_factory, name, seed, pack2):
    path = tmp_path_factory.mktemp(name) / "template.db"
    old = os.environ.get("NETRACARE_DB")
    os.environ["NETRACARE_DB"] = str(path)
    os.environ["CLINIC_NOW"] = CLINIC_NOW
    from app import db
    db.init_db(with_seed_appointments=seed, with_past_visits=pack2)
    if old is not None:
        os.environ["NETRACARE_DB"] = old
    return path


@pytest.fixture(scope="session")
def template_db(tmp_path_factory):
    """Iteration 1 database: data pack 1 + stretch seed appointments (data pack 2 left out so those tests are unchanged)."""
    return _build_template(tmp_path_factory, "tpl1", seed=True, pack2=False)


@pytest.fixture(scope="session")
def template_db2(tmp_path_factory):
    """Iteration 2 database: data pack 1 + data pack 2 (27 past visits, profiles), no seed appointments."""
    return _build_template(tmp_path_factory, "tpl2", seed=False, pack2=True)


def _client(template, tmp_path, monkeypatch):
    fresh = tmp_path / "test.db"
    shutil.copy(template, fresh)
    monkeypatch.setenv("NETRACARE_DB", str(fresh))
    monkeypatch.setenv("CLINIC_NOW", CLINIC_NOW)
    from app.main import app
    return TestClient(app)  # no `with`: lifespan is skipped, the DB is already loaded


@pytest.fixture()
def client(template_db, tmp_path, monkeypatch):
    return _client(template_db, tmp_path, monkeypatch)


@pytest.fixture()
def client2(template_db2, tmp_path, monkeypatch):
    return _client(template_db2, tmp_path, monkeypatch)


@pytest.fixture(scope="session")
def _index_path(tmp_path_factory):
    """Build the REAL Chroma index once per test run (real local embeddings, no key needed)."""
    path = tmp_path_factory.mktemp("chroma")
    os.environ["NETRACARE_INDEX_DIR"] = str(path)
    from app import rag
    rag.build_index()
    return path


@pytest.fixture()
def rag_index(_index_path, monkeypatch):
    from app import rag
    monkeypatch.setenv("NETRACARE_INDEX_DIR", str(_index_path))
    rag.reset()
    yield rag
    rag.reset()


@pytest.fixture(scope="session")
def patients():
    with open(DATA / "patients.csv", newline="", encoding="utf-8") as f:
        return {r["patient_id"]: r for r in csv.DictReader(f)}
