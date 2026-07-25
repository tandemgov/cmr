import pydantic


class Report(pydantic.BaseModel):
    reporting_entity: str
    """The entity creating the report (e.g., Architect of the Capitol)"""

    nature_of_report: str
    """Nature of report (e.g., Expenditures of the group)"""

    authority: str
    """Authority (e.g., 2 U.S.C. 276f; Pub. L. 86-42, Sec. 3; (73 Stat. 73))"""

    when_expected: str
    """When expected to be made (e.g., "If specified circumstances arise")"""
