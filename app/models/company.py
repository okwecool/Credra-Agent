"""Company profile schemas."""

from pydantic import BaseModel, ConfigDict, Field


class CompanyProfile(BaseModel):
    """Normalized company information consumed by analysis and reporting."""

    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(min_length=1)
    industry: str = Field(min_length=1)
    registered_capital: str = Field(min_length=1)
    established_date: str = Field(min_length=1)
    shareholders: list[str]
    business_scope: str = Field(min_length=1)
    major_customers: list[str]
    major_suppliers: list[str]
