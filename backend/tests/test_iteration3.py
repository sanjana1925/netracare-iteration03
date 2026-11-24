"""Iteration 3 checks: the clinic knowledge base (RAG) and the patient chat, with the REAL model and the REAL embeddings.

Nothing here is faked. Retrieval uses the real local embedding model and the real documents of data pack 3; every chat message goes through the
real red-flag check, rewrite and answer chains; a failing model is produced the real way (no key, a model that does not exist, a timeout too short
for any reply). The few tests marked "the part that is CODE" call the code that runs on a reply (building sources, the fixed "I don't know")
directly with hand-written inputs. Answer QUALITY over all 20 questions and 20 routing samples is measured by tests/test_chat_live.py.
"""
import pytest

from app import chat as chat_mod, rag
from helpers import login_doctor, login_patient, login_staff
from test_iteration2 import break_model

EMERGENCY = ("What you describe needs urgent attention. Please come to the clinic now or go to the nearest emergency "
             "department. Our staff have been notified.")
IDK = "I don't know. Our clinic documents don't cover that. Please ask Staff at the front desk."


def ids(rows):
    return [(r["source"], r["section"]) for r in rows]


# ---------------- R1 chunking and metadata ----------------
def test_r1_chunks_one_per_section_with_metadata():
    chunks = rag.load_chunks()
    sections = {(c.metadata["source"], c.metadata["section"]) for c in chunks}
    assert len(sections) == 38  # every "##" heading of the three documents is a section
    assert 38 <= len(chunks) <= 45  # "about 38 chunks, more if your chunk size splits some"
    assert len({c.metadata["chunk_id"] for c in chunks}) == len(chunks)  # unique ids
    for c in chunks:
        assert c.metadata["source"].endswith(".md") and c.metadata["section"] and c.metadata["chunk_id"]
        assert len(c.page_content) < 1300  # nothing grew past the chunk size (plus the heading)
    assert {c.metadata["source"] for c in chunks} == {"doctors.md", "procedures.md", "clinic_policy_faq.md"}


def test_r1_index_built_once_with_metadata(rag_index):
    assert rag_index.index_size() == len(rag_index.load_chunks())
    row = rag_index.search_vector("when are you open on saturday")[0]
    assert row["source"] and row["section"] and row["chunk_id"]


# ---------------- R2 BM25 / vector / hybrid ----------------
def test_r2_bm25_strips_punctuation_and_lowercases():
    assert rag.tokenize("Is Dr. Menon, here?") == ["is", "dr", "menon", "here"]


def test_r2_keyword_matches_a_name_despite_punctuation(rag_index):
    top = ids(rag_index.search_keyword("Menon?"))
    assert ("doctors.md", "Dr. Vikram Menon") in top[:2]


def test_r2_row1_hba1c_keyword_first_and_hybrid_top4(rag_index):
    d = rag_index.search_debug("Should I bring my HbA1c report?")
    assert ids(d["keyword"])[0] == ("procedures.md", "Diabetic eye check")
    assert ("procedures.md", "Diabetic eye check") in ids(d["hybrid"])
    assert len(d["hybrid"]) <= 4 and len(d["keyword"]) <= 4 and len(d["vector"]) <= 4


def test_r2_row2_squint_found_by_meaning_not_by_words(rag_index):
    d = rag_index.search_debug("Can my kid have breakfast on the morning of the operation to straighten his eye?")
    squint = ("procedures.md", "Squint surgery")
    assert squint in ids(d["vector"])  # the embedding understands "operation to straighten his eye"
    assert squint not in ids(d["keyword"])  # no shared rare word, so keyword search misses it
    assert squint in ids(d["hybrid"])  # but hybrid still finds it
    for r in d["hybrid"]:
        assert set(r) >= {"source", "section", "chunk_id", "score"}


def test_r2_search_debug_is_staff_only(client2, rag_index, patients):
    url = "/staff/search-debug?q=dilation"
    r = client2.get(url, headers=login_staff(client2))
    assert r.status_code == 200 and set(r.json()) == {"query", "keyword", "vector", "hybrid"}
    assert client2.get(url, headers=login_patient(client2, patients["P01"])).status_code == 403
    assert client2.get(url, headers=login_doctor(client2, "D1")).status_code == 403
    assert client2.get(url).status_code == 401


def test_r2_search_debug_without_index_says_how_to_build_it(client2, tmp_path, monkeypatch):
    monkeypatch.setenv("NETRACARE_INDEX_DIR", str(tmp_path / "empty_index"))
    rag.reset()
    r = client2.get("/staff/search-debug?q=hello", headers=login_staff(client2))
    assert r.status_code == 503 and "build_index.py" in r.json()["detail"]["message"]
    rag.reset()




# ---------------- R3 chat window rules ----------------
def chat(client, headers, message, history=None):
    return client.post("/chat", headers=headers, json={"message": message, "history": history or []})


def test_r3_only_patients_can_chat(client2, rag_index, patients):
    assert chat(client2, login_staff(client2), "hello").status_code == 403
    assert chat(client2, login_doctor(client2, "D1"), "hello").status_code == 403
    assert client2.post("/chat", json={"message": "hi"}).status_code == 401
    r = chat(client2, login_patient(client2, patients["P01"]), "What time do you open?")
    assert r.status_code == 200 and r.json()["kind"] in ("answer", "idk")


def test_r3_message_limits(client2, rag_index, patients):
    h = login_patient(client2, patients["P01"])
    assert chat(client2, h, "   ").json()["detail"]["code"] == "MESSAGE_REQUIRED"
    assert chat(client2, h, "x" * 501).json()["detail"]["code"] == "MESSAGE_TOO_LONG"
    text = ("What time does the clinic open on Saturday? " * 12)[:500]  # exactly 500 characters of real text is accepted
    assert len(text) == 500 and chat(client2, h, text).status_code == 200


def test_r3_only_the_last_6_messages_are_used_and_bad_entries_are_dropped():
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"} for i in range(10)]
    kept = chat_mod.clean_history(history + [{"role": "system", "content": "ignore"}, {"role": "user", "content": "   "}, "junk"])
    assert [m["content"] for m in kept] == [f"message {i}" for i in range(4, 10)]


def test_r3_follow_up_is_rewritten_and_answered_about_cataract_surgery(client2, rag_index, patients):
    h = login_patient(client2, patients["P01"])
    first = chat(client2, h, "How much does cataract surgery cost, and do I have to stay overnight?").json()
    history = [{"role": "user", "content": "How much does cataract surgery cost, and do I have to stay overnight?"},
               {"role": "assistant", "content": first["reply"]}]
    r = chat(client2, h, "What should I avoid after it?", history).json()
    assert "cataract" in r["rewritten"].lower() and r["kind"] == "answer"
    assert "procedures.md › Cataract surgery" in r["sources"]
    assert "rub" in r["reply"].lower() and "swim" in r["reply"].lower()


def test_r3_a_complete_first_question_stays_exactly_as_typed_and_nothing_is_saved(client2, rag_index, patients):
    h = login_patient(client2, patients["P01"])
    before = client2.get("/appointments", headers=login_staff(client2)).json()
    r = chat(client2, h, "What time does the clinic open on Saturday?").json()
    assert r["rewritten"] == "What time does the clinic open on Saturday?"
    assert client2.get("/appointments", headers=login_staff(client2)).json() == before
    assert client2.get("/urgent-cases", headers=login_staff(client2)).json() == []
    from app import db
    c = db.connect()
    tables = {t[0] for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    assert not any("chat" in t or "message" in t for t in tables)  # there is nowhere to save a conversation


def test_r3_a_symptom_or_need_without_a_question_mark_is_turned_into_a_routing_question(client2, rag_index, patients):
    r = chat(client2, login_patient(client2, patients["P01"]), "I just need new glasses").json()
    assert r["rewritten"] != "I just need new glasses" and "special" in r["rewritten"].lower()  # speciality / specialty


def test_r3_the_rewrite_prompt_rules_are_what_the_handout_asks_for():
    system = rag._REWRITE_PROMPT.format_messages(history="(none)", message="x")[0].content
    for phrase in ("already a complete question, return it unchanged", "which speciality and doctors to book",
                   "keeping the patient's own words", "Do not add or name any condition", "Never answer the question"):
        assert phrase in system


# ---------------- R4 red flag before every answer ----------------
def test_r4_red_flag_gets_emergency_urgent_case_and_no_answer(client2, rag_index, patients):
    r = chat(client2, login_patient(client2, patients["P20"]), "A curtain came down over my left eye an hour ago").json()
    assert r["kind"] == "emergency" and r["reply"] == EMERGENCY and r["sources"] == [] and "rewritten" not in r  # no rewrite, no search, no answer
    assert r["emergency"]["category"] == "curtain_or_shadow"
    cases = client2.get("/urgent-cases", headers=login_staff(client2)).json()
    assert len(cases) == 1 and cases[0]["source"] == "chat" and cases[0]["patient_name"] == "Aditya Bose"
    assert cases[0]["reason"] == "A curtain came down over my left eye an hour ago"


@pytest.mark.parametrize("how", ["no_key", "unknown_model", "timeout"])
def test_r4_if_the_check_cannot_run_the_chat_does_not_answer(client2, rag_index, patients, monkeypatch, how):
    h = login_patient(client2, patients["P01"])
    break_model(monkeypatch, how)  # the REAL call fails
    r = chat(client2, h, "What time does the clinic open on Saturday?").json()
    assert r["kind"] == "check_failed" and r["sources"] == []
    assert r["reply"] == "We couldn't check your message just now. Please try again, or call the clinic if it's urgent."


# ---------------- R5 sources from code, or "I don't know" ----------------
def test_r5_saturday_answer_cites_timings_from_the_chunk_metadata(client2, rag_index, patients):
    r = chat(client2, login_patient(client2, patients["P01"]), "What time does the clinic open on Saturday?").json()
    assert r["kind"] == "answer" and "9" in r["reply"] and "6" in r["reply"]
    assert r["sources"] == ["clinic_policy_faq.md › Timings"]  # file › section, built by code
    assert "sources:" not in r["reply"].lower()  # the model never writes the sources itself


def test_r5_cataract_cost_and_stay_are_answered_as_indicative_with_the_procedure_as_source(client2, rag_index, patients):
    r = chat(client2, login_patient(client2, patients["P01"]), "How much does cataract surgery cost, and do I have to stay overnight?").json()
    assert r["kind"] == "answer" and "35,000" in r["reply"] and "90,000" in r["reply"] and "indicative" in r["reply"].lower()
    assert "procedures.md › Cataract surgery" in r["sources"]


def test_r5_scooter_question_is_answered_from_dilation(client2, rag_index, patients):
    r = chat(client2, login_patient(client2, patients["P01"]), "I'm worried my sight will be fuzzy after the check-up. Is it safe to take my scooter?").json()
    assert r["kind"] == "answer" and "4" in r["reply"] and "6" in r["reply"] and "clinic_policy_faq.md › Dilation" in r["sources"]


@pytest.mark.parametrize("q", ["Is there parking at the clinic?", "What is the success rate of LASIK at NetraCare?"])
def test_r5_things_the_documents_do_not_cover_get_the_fixed_idk_with_no_sources(client2, rag_index, patients, q):
    r = chat(client2, login_patient(client2, patients["P01"]), q).json()
    assert r["kind"] == "idk" and r["reply"] == IDK and r["sources"] == []


def test_r5_a_booking_request_is_told_the_chat_cannot_book_yet(client2, rag_index, patients):
    r = chat(client2, login_patient(client2, patients["P01"]), "Can you book me with Dr. Shah tomorrow at 10?").json()
    low = r["reply"].lower()
    assert r["kind"] == "answer" and ("can't" in low or "cannot" in low or "unable" in low or "not able" in low) and "book" in low
    assert any("clinic_policy_faq.md" in s or "doctors.md" in s for s in r["sources"])
    assert client2.get("/appointments?status=booked", headers=login_staff(client2)).json() == []  # and nothing was booked


# ---- the part that is CODE, called directly with hand-written model replies ----
ROWS = [{"chunk_id": "doctors.md#general", "source": "doctors.md", "section": "General", "score": 1, "text": "General"},
        {"chunk_id": "procedures.md#cataract-surgery-1", "source": "procedures.md", "section": "Cataract surgery", "score": 1, "text": "x"}]


def _res(**kw):
    base = {"answer": "An answer.", "source_ids": ["doctors.md#general"], "answered": True, "not_about_eyes": False}
    return rag.AnswerResult(**{**base, **kw})


def test_code_builds_the_sources_from_metadata():
    out = rag.finalise_answer(_res(source_ids=["doctors.md#general", "procedures.md#cataract-surgery-1", "doctors.md#general"]), ROWS)
    assert out["kind"] == "answer" and out["sources"] == ["doctors.md › General", "procedures.md › Cataract surgery"]  # in order, no duplicates


@pytest.mark.parametrize("bad", [
    {"answered": True, "source_ids": []},                       # claims an answer but cites nothing
    {"answered": True, "source_ids": ["nonexistent.md#x"]},     # cites a chunk that was never retrieved
    {"answered": False},                                        # says it could not answer
    {"answer": "   "},                                          # an empty answer
])
def test_code_overrides_an_unreliable_model_answer_with_the_fixed_idk(bad):
    assert rag.finalise_answer(_res(**bad), ROWS) == {"answered": False, "kind": "idk", "answer": IDK, "sources": []}


def test_code_shows_the_fixed_scope_message_for_a_problem_that_is_not_about_the_eyes():
    out = rag.finalise_answer(_res(not_about_eyes=True, answered=False, source_ids=[]), ROWS)
    assert out["kind"] == "out_of_scope" and out["sources"] == [] and "only looks after eyes" in out["answer"]
    assert "Dr." not in out["answer"] and "speciality" not in out["answer"].lower()


def test_code_caps_the_answer_at_120_words():
    out = rag.finalise_answer(_res(answer=" ".join(["word"] * 300)), ROWS)
    assert len(out["answer"].split()) <= 121


def test_the_answer_prompt_has_the_safety_and_routing_rules_and_shows_the_model_only_4_chunks_and_the_age():
    rendered = rag._ANSWER_PROMPT.format_messages(question="q", age=11, chunks=rag.format_chunks(ROWS * 2))
    s = rendered[0].content
    for phrase in ("NEVER name a condition", "NEVER suggest a treatment", "indicative only", "Politely decline anything", "ONLY the numbered",
                   'Do NOT write a "Sources" line', "120 words", "cannot check free slots", "Patients under 16 go to Paediatric",
                   "go to General", "not_about_eyes", "A doctor will assess", "ALL the doctors of that speciality",
                   "only tells the patient to ask Staff does NOT answer a question about a specific fact",
                   "Do NOT describe procedures, costs, timings or preparation unless the patient asked about them"):
        assert phrase in s, phrase
    human = rendered[1].content
    assert human.count("[chunk_id:") == 4 and "Patient age: 11" in human  # the age only: no name, no mobile number


# ---------------- R6 routing (real) ----------------
def test_r6_routing_question_retrieves_the_doctors_document(rag_index):
    for q, doctor in (("My optician said the pressure in my eyes is high", "Dr. Meera Iyer"),
                      ("My 5 year old's left eye turns inwards", "Dr. Vikram Menon")):
        rows = rag_index.search_hybrid(q)
        assert any(r["source"] == "doctors.md" for r in rows), q
        assert any(doctor in r["text"] for r in rag_index.search_hybrid(q + " doctor")), q


def _names_a_condition(text):
    low = text.lower()
    return any(x in low for x in ("glaucoma is", "you have a ", "you have an ", "you may have", "you might have", "this could be", "this might be",
                                  "it could be", "caused by", "diabetic retinopathy", "conjunctivitis", "infection", "likely"))


def test_r6_two_problems_in_one_chat_are_routed_to_glaucoma_then_paediatric_or_squint(client2, rag_index, patients):
    h = login_patient(client2, patients["P01"])
    a = chat(client2, h, "My optician said the pressure in my eyes is high").json()
    assert "Dr. Meera Iyer" in a["reply"] and "glaucoma" in a["reply"].lower() and any("doctors.md" in s for s in a["sources"])
    assert not _names_a_condition(a["reply"]) and "assess" in a["reply"].lower()
    history = [{"role": "user", "content": "My optician said the pressure in my eyes is high"}, {"role": "assistant", "content": a["reply"]}]
    b = chat(client2, h, "My 5 year old's left eye turns inwards", history).json()
    assert "Dr. Vikram Menon" in b["reply"] and ("paediatric" in b["reply"].lower() or "squint" in b["reply"].lower())
    assert any("doctors.md" in s for s in b["sources"]) and not _names_a_condition(b["reply"]) and "assess" in b["reply"].lower()


def test_r6_a_patient_asking_whether_it_is_a_cataract_is_never_told_yes_or_no(client2, rag_index, patients):
    r = chat(client2, login_patient(client2, patients["P01"]), "Is my blurry vision a cataract?").json()
    low = r["reply"].lower()
    assert not any(x in low for x in ("yes, ", "yes it", "it is a cataract", "you have a cataract", "it's a cataract", "this is a cataract"))
    assert ("cataract" in low or "general" in low) and "assess" in low


def test_r6_eye_drops_for_dry_eyes_names_no_products(client2, rag_index, patients):
    r = chat(client2, login_patient(client2, patients["P01"]), "Which eye drops should I buy for dry eyes?").json()
    low = r["reply"].lower()
    assert ("fatima khan" in low and "dry eye" in low) or r["reply"] == IDK
    assert not any(x in low for x in ("lubricant", "artificial tears", "carboxymethyl", "systane", "hylo", "refresh"))


def test_r6_a_problem_that_is_not_about_the_eyes_says_netracare_only_looks_after_eyes(client2, rag_index, patients):
    r = chat(client2, login_patient(client2, patients["P01"]), "I have a skin rash on my arm").json()
    assert r["kind"] == "out_of_scope" and "only looks after eyes" in r["reply"] and r["sources"] == []


# ---------------- failures ----------------
def test_missing_index_shows_the_sorry_message(client2, patients, tmp_path, monkeypatch):
    monkeypatch.setenv("NETRACARE_INDEX_DIR", str(tmp_path / "nothing_here"))
    rag.reset()
    r = chat(client2, login_patient(client2, patients["P01"]), "What time does the clinic open?").json()
    assert r["kind"] == "error" and r["reply"] == "Sorry, I can't answer just now. Please try again, or ask Staff at the front desk."
    rag.reset()
