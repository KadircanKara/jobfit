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
