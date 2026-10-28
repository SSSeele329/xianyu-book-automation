# -*- coding: utf-8 -*-
"""本地软件支持的固定书源定义。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceDefinition:
    key: str
    label: str
    url: str
    library_suffix: str
    mode: str
    group: str = ""
    legacy_library_suffixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class WeeklyCategory:
    key: str
    label: str
    cat_id: int
    library_suffix: str


UNDERGRAD = SourceDefinition(
    key="undergrad",
    label="大学教材",
    url="https://search.kongfz.com/booklib/category?catId=32024&actionPath=catId&page={page}",
    library_suffix="大学教材",
    mode="paged",
    group="教材",
    legacy_library_suffixes=("大学教材总榜单",),
)

GRADUATE = SourceDefinition(
    key="graduate",
    label="研究生教材",
    url="https://search.kongfz.com/booklib/category?catId=32025&actionPath=catId&page={page}",
    library_suffix="研究生教材",
    mode="paged",
    group="教材",
    legacy_library_suffixes=("研究生教材总榜单",),
)

TEXTBOOK_SOURCES = [
    UNDERGRAD,
    GRADUATE,
    SourceDefinition(
        key="textbook_vocational_college",
        label="高职高专教材",
        url="https://search.kongfz.com/booklib/category?catId=32026&actionPath=catId&page={page}",
        library_suffix="高职高专教材",
        mode="paged",
        group="教材",
    ),
    SourceDefinition(
        key="textbook_vocational_secondary",
        label="中职中专教材",
        url="https://search.kongfz.com/booklib/category?catId=32027&actionPath=catId&page={page}",
        library_suffix="中职中专教材",
        mode="paged",
        group="教材",
    ),
    SourceDefinition(
        key="textbook_adult_education",
        label="成人教育",
        url="https://search.kongfz.com/booklib/category?catId=32028&actionPath=catId&page={page}",
        library_suffix="成人教育",
        mode="paged",
        group="教材",
    ),
    SourceDefinition(
        key="textbook_vocational_training",
        label="职业技术培训",
        url="https://search.kongfz.com/booklib/category?catId=32029&actionPath=catId&page={page}",
        library_suffix="职业技术培训",
        mode="paged",
        group="教材",
    ),
    SourceDefinition(
        key="textbook_public_courses",
        label="公共课",
        url="https://search.kongfz.com/booklib/category?catId=32030&actionPath=catId&page={page}",
        library_suffix="公共课",
        mode="paged",
        group="教材",
    ),
    SourceDefinition(
        key="textbook_economics_management",
        label="经济管理类",
        url="https://search.kongfz.com/booklib/category?catId=32031&actionPath=catId&page={page}",
        library_suffix="经济管理类教材",
        mode="paged",
        group="教材",
    ),
    SourceDefinition(
        key="textbook_engineering",
        label="工学类",
        url="https://search.kongfz.com/booklib/category?catId=32032&actionPath=catId&page={page}",
        library_suffix="工学类教材",
        mode="paged",
        group="教材",
    ),
    SourceDefinition(
        key="textbook_liberal_law",
        label="文法类",
        url="https://search.kongfz.com/booklib/category?catId=32033&actionPath=catId&page={page}",
        library_suffix="文法类教材",
        mode="paged",
        group="教材",
    ),
    SourceDefinition(
        key="textbook_medicine",
        label="医学类",
        url="https://search.kongfz.com/booklib/category?catId=32034&actionPath=catId&page={page}",
        library_suffix="医学类教材",
        mode="paged",
        group="教材",
    ),
    SourceDefinition(
        key="textbook_science",
        label="理学类",
        url="https://search.kongfz.com/booklib/category?catId=32035&actionPath=catId&page={page}",
        library_suffix="理学类教材",
        mode="paged",
        group="教材",
    ),
    SourceDefinition(
        key="textbook_computer",
        label="计算机",
        url="https://search.kongfz.com/booklib/category?catId=32036&actionPath=catId&page={page}",
        library_suffix="计算机教材",
        mode="paged",
        group="教材",
    ),
]

EXAM_SOURCES = [
    SourceDefinition(
        key="exam_civil_service",
        label="公务员",
        url="https://search.kongfz.com/booklib/category?catId=32037&actionPath=catId&page={page}",
        library_suffix="公务员考试",
        mode="paged",
        group="考试",
    ),
    SourceDefinition(
        key="exam_postgraduate",
        label="考研",
        url="https://search.kongfz.com/booklib/category?catId=32038&actionPath=catId&page={page}",
        library_suffix="考研",
        mode="paged",
        group="考试",
    ),
    SourceDefinition(
        key="exam_foreign_language",
        label="外语考试",
        url="https://search.kongfz.com/booklib/category?catId=32039&actionPath=catId&page={page}",
        library_suffix="外语考试",
        mode="paged",
        group="考试",
    ),
    SourceDefinition(
        key="exam_judicial",
        label="司法考试",
        url="https://search.kongfz.com/booklib/category?catId=32040&actionPath=catId&page={page}",
        library_suffix="司法考试",
        mode="paged",
        group="考试",
    ),
    SourceDefinition(
        key="exam_accounting",
        label="会计类",
        url="https://search.kongfz.com/booklib/category?catId=32041&actionPath=catId&page={page}",
        library_suffix="会计类考试",
        mode="paged",
        group="考试",
    ),
    SourceDefinition(
        key="exam_bank",
        label="银行类",
        url="https://search.kongfz.com/booklib/category?catId=32042&actionPath=catId&page={page}",
        library_suffix="银行类考试",
        mode="paged",
        group="考试",
    ),
    SourceDefinition(
        key="exam_teacher",
        label="教师类",
        url="https://search.kongfz.com/booklib/category?catId=32043&actionPath=catId&page={page}",
        library_suffix="教师类考试",
        mode="paged",
        group="考试",
    ),
    SourceDefinition(
        key="exam_medical_pharmacy",
        label="医学/药学",
        url="https://search.kongfz.com/booklib/category?catId=32044&actionPath=catId&page={page}",
        library_suffix="医学药学考试",
        mode="paged",
        group="考试",
    ),
    SourceDefinition(
        key="exam_architecture",
        label="建筑类",
        url="https://search.kongfz.com/booklib/category?catId=32045&actionPath=catId&page={page}",
        library_suffix="建筑类考试",
        mode="paged",
        group="考试",
    ),
    SourceDefinition(
        key="exam_tax_trade_insurance",
        label="财税外贸保险类考试",
        url="https://search.kongfz.com/booklib/category?catId=32046&actionPath=catId&page={page}",
        library_suffix="财税外贸保险类考试",
        mode="paged",
        group="考试",
    ),
    SourceDefinition(
        key="exam_computer",
        label="计算机考试",
        url="https://search.kongfz.com/booklib/category?catId=32047&actionPath=catId&page={page}",
        library_suffix="计算机考试",
        mode="paged",
        group="考试",
    ),
    SourceDefinition(
        key="exam_other",
        label="其他考试",
        url="https://search.kongfz.com/booklib/category?catId=32048&actionPath=catId&page={page}",
        library_suffix="其他考试",
        mode="paged",
        group="考试",
    ),
]

WEEKLY = SourceDefinition(
    key="weekly",
    label="近7天畅销榜",
    url="https://item.kongfz.com/item-views/pc-item/top/seller?type=week",
    library_suffix="近7天畅销榜",
    mode="weekly",
)

PAGED_SOURCES = {
    source.key: source
    for source in [
        *TEXTBOOK_SOURCES,
        *EXAM_SOURCES,
    ]
}

WEEKLY_CATEGORIES = {
    "all": WeeklyCategory(
        key="all",
        label="总榜单",
        cat_id=0,
        library_suffix="近7天畅销总榜",
    ),
    "fiction": WeeklyCategory(
        key="fiction",
        label="小说",
        cat_id=43,
        library_suffix="小说7天畅销榜",
    ),
    "literature": WeeklyCategory(
        key="literature",
        label="文学",
        cat_id=1,
        library_suffix="文学7天畅销榜",
    ),
    "language": WeeklyCategory(
        key="language",
        label="语言文字",
        cat_id=13,
        library_suffix="语言7天畅销榜",
    ),
    "history": WeeklyCategory(
        key="history",
        label="历史",
        cat_id=3,
        library_suffix="历史7天畅销榜",
    ),
    "geography": WeeklyCategory(
        key="geography",
        label="地理",
        cat_id=23,
        library_suffix="地理7天畅销榜",
    ),
    "art": WeeklyCategory(
        key="art",
        label="艺术",
        cat_id=4,
        library_suffix="艺术7天畅销榜",
    ),
    "politics": WeeklyCategory(
        key="politics",
        label="政治",
        cat_id=18,
        library_suffix="政治7天畅销榜",
    ),
    "law": WeeklyCategory(
        key="law",
        label="法律",
        cat_id=5,
        library_suffix="法律7天畅销榜",
    ),
    "military": WeeklyCategory(
        key="military",
        label="军事",
        cat_id=24,
        library_suffix="军事7天畅销榜",
    ),
    "philosophy_psychology": WeeklyCategory(
        key="philosophy_psychology",
        label="哲学心理学",
        cat_id=44,
        library_suffix="哲学心理学7天畅销榜",
    ),
    "religion": WeeklyCategory(
        key="religion",
        label="宗教",
        cat_id=29,
        library_suffix="宗教7天畅销榜",
    ),
    "economics": WeeklyCategory(
        key="economics",
        label="经济",
        cat_id=14,
        library_suffix="经济7天畅销榜",
    ),
    "society_culture": WeeklyCategory(
        key="society_culture",
        label="社会文化",
        cat_id=7,
        library_suffix="社会文化7天畅销榜",
    ),
    "education": WeeklyCategory(
        key="education",
        label="教育",
        cat_id=28,
        library_suffix="教育7天畅销榜",
    ),
    "management": WeeklyCategory(
        key="management",
        label="管理",
        cat_id=25,
        library_suffix="管理7天畅销榜",
    ),
    "children": WeeklyCategory(
        key="children",
        label="童书",
        cat_id=27,
        library_suffix="童书7天畅销榜",
    ),
    "life": WeeklyCategory(
        key="life",
        label="生活",
        cat_id=26,
        library_suffix="生活7天畅销榜",
    ),
    "sports": WeeklyCategory(
        key="sports",
        label="体育",
        cat_id=19,
        library_suffix="体育7天畅销榜",
    ),
    "engineering": WeeklyCategory(
        key="engineering",
        label="工程技术",
        cat_id=11,
        library_suffix="工程技术7天畅销榜",
    ),
    "textbook": WeeklyCategory(
        key="textbook",
        label="教材教辅考试",
        cat_id=32,
        library_suffix="教材7天畅销榜",
    ),
    "computer": WeeklyCategory(
        key="computer",
        label="计算机与互联网",
        cat_id=31,
        library_suffix="计算机7天畅销榜",
    ),
    "natural_science": WeeklyCategory(
        key="natural_science",
        label="自然科学",
        cat_id=15,
        library_suffix="自然科学7天畅销榜",
    ),
    "medicine_health": WeeklyCategory(
        key="medicine_health",
        label="医药卫生",
        cat_id=17,
        library_suffix="医药卫生7天畅销榜",
    ),
    "classics": WeeklyCategory(
        key="classics",
        label="国学古籍",
        cat_id=12,
        library_suffix="国学古籍7天畅销榜",
    ),
    "collectibles": WeeklyCategory(
        key="collectibles",
        label="收藏与鉴赏",
        cat_id=59,
        library_suffix="收藏与鉴赏7天畅销榜",
    ),
    "comprehensive": WeeklyCategory(
        key="comprehensive",
        label="综合性图书",
        cat_id=20,
        library_suffix="综合性图书7天畅销榜",
    ),
}

ALL_SOURCES = {
    **PAGED_SOURCES,
    WEEKLY.key: WEEKLY,
}


def source_key_for_weekly(category_key: str) -> str:
    return f"weekly:{category_key}"


def weekly_url_for(category_key: str) -> str:
    """返回孔网近7天畅销榜指定分类的页面地址。"""
    cat_id = WEEKLY_CATEGORIES[category_key].cat_id
    return WEEKLY.url if cat_id == 0 else f"{WEEKLY.url}&catid={cat_id}"


def library_suffix_for(source_key: str, category_key: str | None = None) -> str:
    if source_key == WEEKLY.key:
        actual_key = category_key or "all"
        return WEEKLY_CATEGORIES[actual_key].library_suffix
    return ALL_SOURCES[source_key].library_suffix
