from enum import StrEnum


class ReviewActionType(StrEnum):
    CONFIRM = "confirm"
    MODIFY = "modify"
    REEVALUATE = "reevaluate"
