from jobhunt.pipeline.client_identity import detect, name_from_domain


def test_a_named_product_with_a_domain_is_found() -> None:
    text = ("Hi — I have an existing SaaS product called Northquill.ai. "
            "The website northquill.ai is live and you can sign up for free.")
    found = detect("Northquill.ai saas repair", text)
    assert found.domain == "northquill.ai"


def test_a_company_that_introduces_itself_is_found() -> None:
    text = "Karma and Luck is looking for an experienced Senior Data & AI Engineer."
    assert detect("Ai - Automation Expert", text).company_name == "Karma and Luck"


def test_a_person_who_introduces_themselves_names_their_company() -> None:
    """The person's own name is not returned - the outreach message greets the
    LinkedIn contact it is sent to, not whoever typed the posting. What this
    pattern is for is the company on the other side of "founder of"."""
    text = "Hi, I'm Oliver, founder of Brightline. We need a backend engineer."
    found = detect("Backend engineer", text)
    assert found.company_name == "Brightline"
    assert not hasattr(found, "person_name")


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


def test_a_brand_plus_qualifier_word_is_still_rejected() -> None:
    assert detect("x", "our company, Zoom Inc, and we are hiring a dev").company_name is None


def test_a_brand_named_with_a_leading_article_is_still_rejected() -> None:
    assert detect("x", "The Zoom team is looking for a contractor").company_name is None


def test_a_noise_brand_in_the_title_is_rejected() -> None:
    found = detect("Slack app is looking for a developer", "we need a backend build")
    assert found.company_name is None


def test_multi_word_names_with_no_noise_word_still_survive() -> None:
    text = "Karma and Luck is looking for an experienced Senior Data & AI Engineer."
    assert detect("x", text).company_name == "Karma and Luck"


def test_a_three_word_name_with_no_noise_word_still_survives() -> None:
    text = "Booz Allen Hamilton is looking for a cleared engineer."
    assert detect("x", text).company_name == "Booz Allen Hamilton"


def test_a_person_affiliated_with_a_noise_company_is_dropped_too() -> None:
    text = "this is Alex from Zoom, and we want a Zoom plugin built for internal use."
    assert detect("x", text).company_name is None


def test_name_from_domain_strips_the_tld_and_title_cases() -> None:
    assert name_from_domain("northquill.ai") == "Northquill"
    assert name_from_domain("acme-labs.io") == "Acme Labs"


def test_a_hyphen_joined_noise_brand_is_rejected() -> None:
    assert detect("x", "our company, Zoom-Labs, and we are hiring a dev").company_name is None


def test_a_possessive_noise_brand_is_rejected() -> None:
    assert detect("x", "The Zoom's team is looking for a contractor").company_name is None


def test_a_slash_joined_noise_brand_is_rejected() -> None:
    assert detect("x", "our company, Zoom/Labs, and we are hiring a dev").company_name is None


def test_a_longer_word_sharing_a_prefix_with_a_brand_still_survives() -> None:
    found = detect("x", "Zoomer Labs is looking for a contractor")
    assert found.company_name == "Zoomer Labs"


def test_a_standalone_x_token_no_longer_trips_the_x_dot_com_brand() -> None:
    found = detect("x", "X Corp is looking for a contractor")
    assert found.company_name == "X Corp"


def test_the_platform_a_gig_deploys_to_is_never_the_client() -> None:
    """Domain outranks every other signal, and `get_or_create_company` dedupes on
    domain - so a gig that mentions vercel.com used to become a Vercel gig and
    merge into the real Vercel company row."""
    for host in ("vercel.com", "supabase.com", "railway.app", "make.com", "pinecone.io"):
        text = f"We need help with our app; it deploys to https://{host} already."
        assert detect("Backend developer", text).domain is None, host


def test_a_real_client_domain_still_wins_over_the_stack_it_names() -> None:
    text = (
        "We are Northquill (northquill.ai). Our app is hosted on vercel.com and "
        "our data lives in supabase.com."
    )
    assert detect("RAG engineer", text).domain == "northquill.ai"


def test_a_www_prefix_is_never_the_company_name() -> None:
    """Live: "www.cb-holistictherapy.ie" became the company "Www". Two faults at
    once - the `www.` label was treated as the name, and `.ie` was not a TLD this
    module knew, so it fell back to the first label instead of the last-but-one."""
    assert name_from_domain("www.cb-holistictherapy.ie") == "Cb Holistictherapy"


def test_an_unlisted_tld_still_yields_the_name_before_it() -> None:
    assert name_from_domain("northquill.se") == "Northquill"


def test_a_two_part_public_suffix_is_not_mistaken_for_the_name() -> None:
    """The reason the TLD set exists: naively taking the last-but-one label
    would call this company "Co"."""
    assert name_from_domain("acme-labs.co.uk") == "Acme Labs"


def test_a_bare_single_label_is_returned_as_is() -> None:
    assert name_from_domain("acme") == "Acme"


# --- the product a client wants built -----------------------------------------


def test_a_product_the_client_wants_built_is_a_company_lead() -> None:
    """Verbatim from a live posting. The client is anonymous, and this sentence
    is the only thing in 1,500 words that names anyone."""
    identity = detect(
        "Build Nexora AI Operations Hub – A Multi-Agent AI Business Operations System",
        "We are looking to build Nexora AI Operations Hub, a multi-agent AI business "
        "operations platform designed to intelligently automate complex processes.",
    )
    assert identity.company_name == "Nexora"


def test_the_product_name_is_trimmed_to_what_can_actually_be_searched() -> None:
    """Measured against LinkedIn: "Nexora AI Operations Hub" returned nobody at
    Nexora, "Nexora AI" two people, "Nexora" four. The descriptive tail is what
    the client is building, not what anyone puts in their headline."""
    identity = detect("t", "We are building Aurora Data Platform, a warehouse tool.")
    assert identity.company_name == "Aurora"


def test_a_generic_thing_to_build_is_not_a_name() -> None:
    identity = detect("t", "We are looking to build a multi-agent AI platform for ops.")
    assert identity.company_name is None


def test_a_stated_company_is_not_trimmed() -> None:
    """Trimming is for a product description. A company that introduces itself
    said its own name, and dropping half of it would be inventing a new one."""
    identity = detect("t", "We're Northquill Labs, and we need a backend engineer.")
    assert identity.company_name == "Northquill Labs"


def test_a_stated_company_still_outranks_a_product_name() -> None:
    identity = detect(
        "t",
        "We are looking to build Nexora Hub, a platform. Our company, Vantage Systems, "
        "has been at this for years.",
    )
    assert identity.company_name == "Vantage Systems"


def test_a_domain_still_outranks_a_product_name() -> None:
    identity = detect("t", "We are building Nexora Hub, a platform. See northquill.ai for us.")
    assert identity.domain == "northquill.ai"


def test_a_product_name_that_is_only_a_generic_word_is_rejected() -> None:
    assert detect("t", "We are building Platform Hub, a tool.").company_name is None


def test_a_sentence_initial_introduction_is_matched() -> None:
    """These patterns were anchored to lowercase lead-ins, so the form a posting
    actually opens with - "We're Acme," at the start of a sentence - never
    matched, while the mid-sentence "we're Acme," did."""
    assert detect("t", "We're Northquill, and we need help.").company_name == "Northquill"
    assert detect("t", "Our company, Vantage, is hiring.").company_name == "Vantage"
    assert detect("t", "I'm Sarah, founder of Northquill.").company_name == "Northquill"


def test_the_lowercase_forms_still_match() -> None:
    assert detect("t", "Hi, we're Northquill, and we need help.").company_name == "Northquill"
