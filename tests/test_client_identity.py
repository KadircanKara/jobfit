from jobhunt.pipeline.client_identity import detect


def test_a_named_product_with_a_domain_is_found() -> None:
    text = ("Hi — I have an existing SaaS product called Northquill.ai. "
            "The website northquill.ai is live and you can sign up for free.")
    found = detect("Northquill.ai saas repair", text)
    assert found.domain == "northquill.ai"


def test_a_company_that_introduces_itself_is_found() -> None:
    text = "Karma and Luck is looking for an experienced Senior Data & AI Engineer."
    assert detect("Ai - Automation Expert", text).company_name == "Karma and Luck"


def test_a_person_who_introduces_themselves_is_found() -> None:
    text = "Hi, I'm Oliver, founder of Brightline. We need a backend engineer."
    found = detect("Backend engineer", text)
    assert found.person_name == "Oliver"
    assert found.company_name == "Brightline"


def test_an_anonymous_posting_yields_nothing() -> None:
    text = "I need a tool developed to evaluate property values from images."
    found = detect("full-stack AI developer", text)
    assert found.domain is None and found.company_name is None


def test_upworks_own_domain_is_never_the_client() -> None:
    text = "Apply at https://www.upwork.com/jobs/~021234 for this role."
    assert detect("x", text).domain is None


def test_a_common_platform_domain_is_not_the_client() -> None:
    for noise in ("github.com", "figma.com", "docs.google.com", "openai.com", "zoom.us"):
        assert detect("x", f"See {noise} for details").domain is None


def test_an_email_domain_counts() -> None:
    assert detect("x", "Reach me at sam@brightline.io").domain == "brightline.io"


def test_no_description_is_not_an_error() -> None:
    assert detect("x", None).domain is None


def test_a_noise_brand_named_as_a_company_is_rejected() -> None:
    text = "this is Alex from Zoom, and we want a Zoom plugin built for internal use."
    found = detect("x", text)
    assert found.company_name is None


def test_this_is_matches_regardless_of_capitalisation() -> None:
    text = "This is Alex from Acme, we need a backend engineer."
    found = detect("x", text)
    assert found.person_name == "Alex"
    assert found.company_name == "Acme"


def test_a_captured_name_cannot_span_a_sentence_and_the_domain_wins() -> None:
    text = "I am building on northquill.ai. Slack is looking for nothing. we are Northquill."
    found = detect("x", text)
    assert found.domain == "northquill.ai"
    assert found.company_name is None


def test_an_integration_target_named_in_passing_is_not_the_client() -> None:
    found = detect("x", "we need this to sync with Salesforce")
    assert found.company_name is None
    assert found.domain is None


def test_a_title_naming_the_product_is_read() -> None:
    found = detect("Northquill.ai saas repair", "I need help fixing a bug in my app.")
    assert found.domain == "northquill.ai"
