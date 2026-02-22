import json

from django.test.client import Client


def test_classify_endpoint_success(client: Client) -> None:
    payload = {
        "study_plan": [
            {"dept": "CS", "no": "101", "marks": "", "letter": "A"},
            {"dept": "CS", "no": "102", "marks": "", "letter": "F"},
        ],
        "timetable": ["CS102"],
    }
    response = client.post(
        "/classify/",
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert response.status_code == 200
    body = response.json()
    assert "CS101" in body["passed"]
    assert "CS102" in body["studying"]


def test_parse_and_classify_endpoint_success(client: Client) -> None:
    study_html = """
    <html><body>
    <table dir='rtl'>
      <tr><th>LEVEL 1</th></tr>
      <tr>
        <td>A</td><td>90</td><td>3</td><td>101</td><td>CS</td><td>Intro</td>
      </tr>
    </table>
    </body></html>
    """
    timetable_html = """
    <html><body>
    <table class='forumline'>
      <tr><th>Course</th></tr>
      <tr><td>x</td><td>x</td><td>CS</td><td>101</td></tr>
    </table>
    </body></html>
    """
    payload = {"study_plan_html": study_html, "timetable_html": timetable_html}
    response = client.post(
        "/parse-and-classify/",
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["study_plan_count"] == 1
    assert body["timetable_count"] == 1
    assert "CS101" in body["classification"]["passed"]
