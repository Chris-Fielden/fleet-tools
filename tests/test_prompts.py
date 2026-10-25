from fleet import launch


def test_prompts_say_never_wait():
    for p in (launch.PROMPT, launch.ONCALL_PROMPT,
              launch.RESEARCH_PROMPT):
        assert "never stop to ask" in p
        assert "safer option" in p
