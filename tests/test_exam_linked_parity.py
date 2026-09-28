"""With no links, every engine function returns exactly what master returned.

Linked exams reach nearly every solver: the scheduler, the feasibility check,
the invigilator post-pass, the QA, "Fix with fewest moves", Optimise, Check and
multistart. With no links each one must return, byte for byte, what it returned
on origin/master (d6d9c13) before links existed.

The boards live in ``exam_linked_parity_corpus``. The digests below were
recorded by running those very boards through master's own functions, under
three different PYTHONHASHSEED values, which agreed. A board whose digest no
longer matches is a behaviour change without links - which this feature may
not make. Re-recording them from the branch would defeat the test.

Pure boards keep a 16-hex-digit prefix per board, so a failure names the
boards that changed; the database population keeps whole digests.
"""

import functools
from types import SimpleNamespace

import pytest

from core import exam_views, models
from core.services import exam_input_fingerprint, exam_min_change, exam_multistart, exam_timetable
from core.services.exam_evaluation import evaluate_exam_schedule
from core.services.exam_multistart import CandidateMetrics, report_to_dict, run_multistart
from core.services.exam_room_allocation import RoomAllocationContext
from core.services.linked_exams import NO_LINKS
from tests import exam_linked_parity_corpus as corpus

GOLDEN = {
    "schedule": """
        b0d63bcec21138d4 c08459a6384bb4fa 4ce8a792bf19ae1e 63da266bcdc4b24f fe42bccad6e04d79
        7fdc4549b1af619b ce42a503e7d6b417 b37eaff765c0a04e 4cd6c36dab688dce e914e8cdb284e234
        e5fbd0c9dcd31431 7a9772bdbbf1cf12 3d0bac5f864dd6c5 58511b0245f7cfc7 1f0821e2fef3f3b7
        7ec742747561798a 79e61c3d46d4d69d 679428a4b36767e3 b2cbde947739d529 528dcc1b1cee23d6
        d009969695d9011f 37371bf7300fed95 8edff301146fbb84 23c8f8f0748c72aa 312dd102cf29a374
        3e46f310e7a7e9f3 eb53288770ee0453 7ff623d953c0e5cd 1b3a2be4c185b2f7 ecef021380c016a2
        f3defcb04306db09 c53efafaf4513927 61340fcebd8f12a4 bf08a23da3290155 be5aaa3696ad4fef
        58ac19f9e5a48ea8 0f21694236079d6b a0a0980edc0c4715 46646ef72acd24fd 0d27337d0106c5b7
        e89d58694f3d866f 67b3a03ba6e57f14 0bf68610ef3b5323 4946cc0305ca8820 2982d0501853bcc2
        a9e8872205a6d9be 284af8b62e0d6a60 c7bc0f6afb845244
    """.split(),
    "qa": """
        782e39a1dc1649f5 8683853aa6f8b1d4 8cf591909ae01990 e2569b741018d23b 777323171e284690
        b845defdbefcdfac 1ba10d1ab67f071c a78ebfdff6c4d55e 739d24531637423a 387a71eb4c4a8612
        c7c75dd73cc56b6b f291223965479ce2 cfa3c76f0ce96a9e 81fbfb1ebeef2e19 16a657ccc979fcaa
        4724ff1c34157213 54e51dc110abef27 ca61fa6b8c54251e bf02f5409b79085e c93ba72b51b7d2de
        cb233f6ad2262da8 045e7d6787c3cf7c 07713702a6763c89 26e58e7c880bab98 9c26fe5c51bbabca
        86b860f367990f5d c4b4523bdeb942e9 6bfa4739f2b04dfd af8b6f9ad55d7a43 4f24a777436ba1da
        7e0db431dc65f336 0d69fd2f24bddebe a571e8e88e0d97aa 9ee53e6b98a8711a 73fb91b5c54fb80d
        b00ddbc4d2dafbce f9a3f08be6eda8a8 1d1a5ffcd65dfdde ee2864f0ad017d6b 486a75f1c37e25c0
        9fcb2f85b8083239 c14243311de6a323 ae1e9299b03f3f52 3d3d9858a2ec4dd7 1a7e5befee9d0b6d
        d93aadd2dde9bf65 bbd53904386a5fd5 4ab5d110f1b6761f
    """.split(),
    "post_pass": """
        2a5573c2ad068182 a083b612190fdb6e 20bec06ba3ac6e21 949ebfa5c35784e1 496f01fcd44a72da
        338875c8dee0a413 9d44307cd2129143 e9350ea0f32bd6d0 4a7f2792c6a40537 e565421b9f0a89c0
        c9b97cf68ce98734 94e9e1c681dc6495 6f0865cc0d209e06 6cbeacd113b64bbd fb05ff804f87b24d
        e21acc72a2ad134f 99b9cd100dc8d777 9aa4fb9a93a8e716 3a08cd14cf4ec1e7 8b4a0c3cddcf3e4f
        bb5f97ca95620d5c 43e7a79939460ba3 847504147cf95f5a 47194cdc2ceaa5ea 586ab5996690e002
        f1a0d866b7978d6b 3252ffc152cb568c 3ce3ae91679a538e adc2d86d4c7a4dab 15a594e9226ddcea
        16c4828d6c8770de 7785f0ce2c17fac3 3d7c857a24bf0981 be1e6d2f0cb6277f 252f27bd8b6a0622
        1851ad3acb3d1044 e1f8b6f4996ddf56 b0b1ab7257b85411 89ddcaa709184252 f78074b65b129cc2
        926936cc3301c4e4 c6b72a6dbe8dd283 bbf57a5fe72b6b16 5872c2977d877624 64687889dc785bc0
        bdeef97dac243da6 28043972da5aa72f 41428608b6eb440e
    """.split(),
    "repair": """
        962f393418804da0 ba95553ba0b6d9f7 fb49a4ed729a9440 0da4071d185277fc 131de518ea9aa29c
        f42d0511961c8baa 82009ae33fcb6774 59e1f0d912cdfa5b e8485db4e2bb5a9d b6d85f10b0b56c1f
        0229559fbe2d4e34 4be1d8000746d6f8 9a0625271593fc06 6f5da22bd3990c57 ce3360cf47b85d7c
        a1666be863cbc442 52acd89d35dfa7a8 a2999323bc2c690a f22b2893ffe7b51d c205fdb54dbf97ce
        41a9ba3c5dbefc73 57dd8b73c4d39493 89904b5d10926725 b773855d06ffbfff e30124c1dfb15af6
        a4ce9db31cf6aa27 220d6054aef670f4 76d4584cc81d17bf c1b85fee50f6a761 1e3a1eea16a3cc61
        aa9e17573e6b4be1 aab48866ff8d08f0 a2ec7f41b721ed43 05e915e9098d02ed eaefd26c8004b08a
        fc21cc162527bcec 9992d8281dcf5abe 9e0652bcfa6fda6f 9f90c2237d819dbd 0edf1ee5148b763d
        0dc3ce304417f8b0 96127a46fdfbb6a4 71ccd930af254076 d863ed889c8fd06f f1a250c8256a9874
        1d60c69f55cf18b7 4356bdecfa1e6b5e cbf27b55bf63b2c4 c19d413fd3448d12 5f3c459edd8799c8
        c5a524306350d58d f1d3ac0e7116eef5 babfb907a09d2b0c 3ab914e73303bbc6 eb994b27feac5bfa
        1be808ea2a4d75e3 5c09fea9504c8281 9f3aa5cfc8aed6ad ed52754ac590ca6c a54f7a7c08092409
        4a2ddfc075006f09 41acf5a4f03e72b8 e9af5b27176dfa5f 9438ea1af90db920 44c05e67113c4071
        d4c392c749bb8645 62cdc92128d1b09b 97a149eb3becc488 02d8ffc8b4632aab 71e62c44529625a1
        b6278baed3997ef5 fd6642455457cb07 2144052ffff35530 3d896f2e1f0bae5f 95123429285a4a77
        bf6bfe9889d655b3 a443644eec4a3bd8 ccbc91a21bebe232 e1280a1cbaff5008 afe3d611e9a41ecc
        c9fd9e157427c1f4 13dde5576ee675df 5a4d78c6ad25e693 a113f0cd2e57152b 2d5e662d2bba0ceb
        5c28585ab55ae76b ab521dc19d4c2fc9 7d8905959a80b880 76ef9008324dad08 139ecf5e46ba4b80
        53560777b559bc1e 80c4c374b1f33e87 079140c676d21a98 b3c210ea7177037f eec1105455b77c90
        a61149781673b326 a116e4790e75c630 c104cafacae3c4ce 280b65562322dfb3 65361b227a4e3708
        fc4604ab8a560650 dd02f9ed2f2ebda4 58ae3bb89d756515 9bb9526137420177 e93235bac381b875
        ee1aae00b93603e2 4e10ef5046c64f31 01782069b0bda568 d4b8d33cea9e7495 8ef9ef29bbb3e4a7
        17f7b4551d1ccef9 2dd2625ad8f834b4 38c8814ef57d3f09 f7fb7084df39b7dd a3401e547424115c
        672ad7a9f2d6e384 6a056b2beaa26e64 7da8d996b7cb45a1 d8903aa55102b03e 45271897cd73f012
        51c3fe0b5e7fd484 ac7bcceaee9b1e6d d9d05ff65197c8dc 0247fa2a980a6d61 cc2bebf08000fe01
        aa0e82e5cd9d2418 31a9ddbaa0ed0cc1 15d06f2f53b55fa5 bfb7c39c37b4ac3b c82ca5a1879bd50c
        141e19a86f98426e e54e5faff14ad256 1cbc17a91a1da799 653cc20ce751467b 0e675dac64376073
        a4244ae2063835dc 86d8297002321040 01a97dd7c0f2826c 4849aefa90a6f4b1 64b293e9bbdd2bab
        02521bc950d2416f 5c99a7a575cca0c4 1c69c918efc8c963 7e192920f1b60aed 76b846899b970500
        7d8cb7092f28435a 2886c397209d903d e1a6c668a65c9753 0b14650c79311f1c 55b942873da933ab
        01c6fa3d1318a33e f69199d0485b2967 e2a7d014db9606ca df7a9a3742ce3aa3 b70a5c3756f2e549
        8471a5eea60c9616 7abc30a108b710b2 e62a5153b6543b66 b511cc11fea3a9c8 2e78673e9cb7fff1
        c558d83d2a768fbe d386a9c6d36115ff 100d5484c6e6144b 210b988ca50323fc 6947bd46306ab4f7
        797d9e0c70b0ca66 c742fa9b91eeb28a 648263c72a8033b9 ab6e404f1f042920 ab3d882d08f8ff02
        2bfa04f11320b6d3 79bc3d8cc712496b b161c9406b8fb745 bae5affd275b911f 080ba7fbe8191bab
        28ca67723e91fb7f aa045e3689bf9330 dbb19c4543302c18 3a8b43059a2b986f 3d35c8e1d1d888e0
        e2360501cbecbe6a fbcfe2370e3b960f 16346b7dbdedc0f1 88f2b88f217e950c b85ec0c38021a8a7
        ec6a0b96371c7691 d8e6699aee5a39a0 5a0a2eebea5179bb 575b05d2831757ae 04c32b9e1e4b68ef
        aa6f98ea94193348 f36b9eebfec1f153 72841f7a84921ac2 37947f537320f15f c71961cd55069f7a
        0c2f5c605fa6a271 ddc1f218861a71f0 8372cb11a1c48fa7 a20f09b5558bd603 15d39a646ce4c6d1
        2f251a5a1c1e114f 931c1bd6c561770f 954bce319cc06366 1d978c9493c2d6b5 23b67f4d8db6dd1e
        39a2bad186e82b45 aebccf24fb56ac6f 14a0531307c6b886 770473d847ee7fd3 a80ed7ebbbd5fe74
        5a786c995b0881bd 8d032611a8942c6c 19e16a61531c6cd4 11c9122f2b6119c4 3f09518ea0e2398d
        c69adc73a85b0794 81931790d99717f4 5347dad194a494c9 2851475217d78547 c5c35268bb28d60c
        5e287090e44600ef 76da2168b8c67fde d0d7f79618353732 6697653f853a25b0 8f09efce49e47d35
        f8f16079ed9cbf60 08d8fff69602f6e2 088c4cb5cbed8461 895c0b2209f6661c 01c5ea8e62504c8f
    """.split(),
    "fix": """
        111167a4e6184ecf c525df05a39ad4be 3b1722a4705efbd7 d2fd70dc4a235a42 69e24398d0e4bb1e
        aa8ac945099cb1d3 c525df05a39ad4be eea09737dc3720e5 99bafabe619160a1 4ba606df94ceac90
        b25bb52cf3a22d90 03ad388ac85d73a6 9e0dedd70a070647 2c57d7b511f33c54 4a5c33f144126f62
        3c3d2d0d07213e09 0f31f74d3d8be3e7 190092779881c608 be27145fdd3a2100 8be169f8643b477f
        756edc0dfcf92678 d2fd70dc4a235a42 583c4d339683db27 ee94db923f75c0f3 c9344ee16ff0784b
        dfc9d6cc837a393f 68d75c2f4fcd38c4 f8313fe05d972ab9 202db19f3af6161e b3f75879be7edb8d
        6ca1a5cb940e1bff 8fccf9a0aeabfe20 7572e6550eeb888a c8d50698c6162bb3 b44d56955e02e2ae
        1e16c0a734bcc094 90e45ff0c6f36940
    """.split(),
}
MASTER_POPULATION = {
    "build": "6ba1d5514dd96ff6491fd6b47ef34220791b8a42cb93feca6ec4865615881bdd",
    "check": "5ef7c6e5c6176fbf63966241e4b1024cbcc9d8c2f7b35fa0213b270c0c843933",
    "optimise": "406aeca50c9b22bb4712370d48621127f24ee51d35d9bfe18135839014a46f83",
    "fix": "1ff7dfff7d0d3dc160789efee2e2849cb25fd930eecab0754d6c34c0995011e1",
    "multistart": "ded1b50dcf782a4cf2b3767e046255e4f9b3466bda1a7154e7f8d4d90cf79902",
}
#: The one deliberate change: multistart now ranks candidates on the room QA a
#: build actually writes (``qa["rooms"]``), where master read two keys from the
#: top level of ``qa`` that no build writes and scored every candidate 0
#: unseated and 0% utilisation. On this population it moves two roles
#: (lowest_overload, best_room_feasibility) from seed 3 (utilisation 0.713) to
#: seed 5 (0.7409). ``test_multistart_differs_from_master_only_by_its_room_metrics``
#: proves nothing else moved: with master's reader put back, every digest -
#: multistart included - is master's again.
POPULATION = {
    **MASTER_POPULATION,
    "multistart": "c582c74d413b8998bd1f1bab825763605fd6d6fb5ae328346c9c1c35024e7573",
}


#: Master's room-allocation policy version. Version 2 lets linked courses share
#: a room; without links it allocates exactly as version 1 did, and the bump
#: reaches a result only through its input fingerprint. The database tests pin
#: the fingerprint's copy to master's value, so every other byte is compared.
MASTER_ROOM_POLICY = 1


@pytest.fixture
def masters_room_policy(monkeypatch):
    monkeypatch.setattr(
        exam_input_fingerprint, "ROOM_ALLOCATION_POLICY_VERSION", MASTER_ROOM_POLICY
    )


def _master_extract_metrics(payload):
    """``exam_multistart._extract_metrics`` exactly as master had it."""
    qa = payload.get("qa") or {}
    schedule = payload.get("schedule") or []
    return CandidateMetrics(
        overflow_count=sum(1 for e in schedule if e.get("day") == "OVERFLOW"),
        students_over_limit_count=int(qa.get("students_over_limit_per_day", 0)),
        heavy_day_students=int(qa.get("heavy_day_students", 0)),
        same_slot_conflicts=int(qa.get("conflict_count", 0)),
        bucket_day_violations=int(qa.get("bucket_day_violations_count", 0)),
        unassigned_room_sections=int(qa.get("unassigned_room_sections", 0)),
        multi_sitting_sections=int(qa.get("multi_sitting_sections", 0)),
        avg_utilisation=float(qa.get("avg_utilization", 0.0)),
        max_credit_load_per_day=int(qa.get("max_credit_load_per_day", 0)),
        max_exams_per_day=int(qa.get("max_exams_per_day_per_student", 0)),
    )


def _assert_master(kind: str, digests: list[str]) -> None:
    expected = GOLDEN[kind]
    assert len(digests) == len(expected), f"{kind}: the corpus itself changed"
    changed = [
        number
        for number, (digest, recorded) in enumerate(zip(digests, expected, strict=True))
        if digest[:16] != recorded
    ]
    assert not changed, f"{kind}: boards {changed} no longer return what master returned"


def _graphs():
    for board in corpus.pure_boards():
        _conflicts, adj, thin = corpus.graph(
            board, exam_timetable.build_conflict_graph, exam_timetable.apply_thin_conflict_policy
        )
        yield board, adj, thin


@pytest.mark.parametrize(
    "scheduler",
    [
        exam_timetable.schedule,
        functools.partial(exam_timetable.schedule_linked, links=NO_LINKS),
    ],
    ids=["schedule", "schedule_linked"],
)
def test_the_scheduler_matches_master_without_links(scheduler):
    """Placements, Extra-n numbering and every progress call: seeds, pins,
    preferred slots, credit maps (none, empty, mixed) and too few slots."""
    _assert_master(
        "schedule", [corpus.run_schedule(board, scheduler, adj) for board, adj, _ in _graphs()]
    )


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "no-links"])
def test_the_qa_matches_master_without_links(explicit):
    """Clashes, bucket days, loads, same-day pairs, thin relaxation, the
    student-load signature - on boards with clashes and OVERFLOW."""
    extra = {"links": NO_LINKS} if explicit else {}
    build_qa = functools.partial(exam_timetable._build_qa, **extra)
    attach = functools.partial(exam_timetable.attach_exam_relaxation_qa, **extra)
    _assert_master(
        "qa",
        [
            corpus.run_qa(board, build_qa, attach, exam_timetable.student_load_signature, thin)
            for board, _adj, thin in _graphs()
        ],
    )


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "no-links"])
def test_the_invigilator_pass_matches_master_without_links(explicit):
    """Accepted moves, final placements and rooms, and every trial counted."""
    rebalance = functools.partial(
        exam_timetable._rebalance_invigilators_pass, **({"links": NO_LINKS} if explicit else {})
    )
    _assert_master(
        "post_pass",
        [
            corpus.run_post_pass(
                board,
                exam_timetable.assign_rooms_to_schedule,
                rebalance,
                RoomAllocationContext.for_periods,
                adj,
            )
            for board, adj, _thin in _graphs()
        ],
    )


@pytest.mark.parametrize("weights", [None, {}], ids=["default", "no-weights"])
def test_fewest_moves_matches_master_without_links(weights):
    """The PR #112 brute-force corpora, then boards that widen and overflow."""
    repair = (
        exam_min_change.repair_minimum_change
        if weights is None
        else functools.partial(exam_min_change.repair_minimum_change, weights=weights)
    )
    _assert_master(
        "repair",
        [
            corpus.run_repair(board, repair)
            for board in corpus.repair_boards(exam_min_change.find_violations)
        ],
    )


@pytest.mark.parametrize("extra", [None, {"linked_exams": []}], ids=["default", "no-links"])
def test_the_fix_view_matches_master_without_links(monkeypatch, extra):
    """Edited boards, pins, carried protection and exams already in OVERFLOW:
    the repaired board, the report, and what is handed to the save."""
    patch = functools.partial(monkeypatch.setattr, exam_views)
    _assert_master(
        "fix",
        [corpus.run_fix(board, exam_views, patch, extra) for board in corpus.fix_boards()],
    )


@pytest.mark.django_db
@pytest.mark.parametrize("extra", [None, {"linked_exams": []}], ids=["default", "no-links"])
def test_build_check_optimise_fix_and_multistart_match_master_without_links(
    extra, masters_room_policy
):
    """A real build with rooms, a pin, a two-identity code and the invigilator
    pass; its Check; Optimise from it; a drag repaired by Fix; and multistart."""
    corpus.create_population(models)
    api = SimpleNamespace(
        build_exam_timetable=exam_timetable.build_exam_timetable,
        evaluate_exam_schedule=evaluate_exam_schedule,
        views=exam_views,
        run_multistart=run_multistart,
        report_to_dict=report_to_dict,
    )
    assert corpus.run_population(api, extra) == POPULATION


@pytest.mark.django_db
@pytest.mark.parametrize(
    "teaching", corpus.TEACHING_APART.values(), ids=list(corpus.TEACHING_APART)
)
def test_every_exam_path_seats_by_exam_seats_whatever_the_teaching_capacity(
    teaching, masters_room_policy
):
    """Exams seat by ``Room.exam_capacity`` (owner, 2026-09-28). Each room's
    exam seats are master's capacities and its teaching capacity is set apart -
    to none, and to far more: Build, Check, Optimise, Fix and multistart still
    return master's digests, input fingerprints included."""
    corpus.create_population(models, teaching=teaching)
    api = SimpleNamespace(
        build_exam_timetable=exam_timetable.build_exam_timetable,
        evaluate_exam_schedule=evaluate_exam_schedule,
        views=exam_views,
        run_multistart=run_multistart,
        report_to_dict=report_to_dict,
    )
    assert corpus.run_population(api) == POPULATION


@pytest.mark.django_db
def test_multistart_differs_from_master_only_by_its_room_metrics(monkeypatch, masters_room_policy):
    """Put master's metric reader back and the whole population is master's."""
    corpus.create_population(models)
    monkeypatch.setattr(exam_multistart, "_extract_metrics", _master_extract_metrics)
    api = SimpleNamespace(
        build_exam_timetable=exam_timetable.build_exam_timetable,
        evaluate_exam_schedule=evaluate_exam_schedule,
        views=exam_views,
        run_multistart=run_multistart,
        report_to_dict=report_to_dict,
    )
    assert corpus.run_population(api) == MASTER_POPULATION
