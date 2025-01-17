from mongoengine import Document, StringField

from database import Database, strict
from database.fields import SafeDictField
from database.model import DbModelMixin
from database.model.company import Company


class User(DbModelMixin, Document):
    meta = {
        'db_alias': Database.backend,
        'strict': strict,
    }

    id = StringField(primary_key=True)
    company = StringField(required=True, reference_field=Company)
    name = StringField(required=True, user_set_allowed=True)
    family_name = StringField(user_set_allowed=True)
    given_name = StringField(user_set_allowed=True)
    avatar = StringField()
    preferences = SafeDictField(default=dict, exclude_by_default=True)
