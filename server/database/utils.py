import hashlib
from inspect import ismethod, getmembers
from uuid import uuid4

from mongoengine import EmbeddedDocumentField, ListField, Document, Q
from mongoengine.base import BaseField

from .errors import translate_errors_context, ParseCallError


def get_fields(cls, of_type=BaseField, return_instance=False):
    """ get field names from a class containing mongoengine fields """
    res = []
    for cls_ in reversed(cls.mro()):
        res.extend([k if not return_instance else (k, v)
                    for k, v in vars(cls_).items()
                    if isinstance(v, of_type)])
    return res


def get_fields_and_attr(cls, attr):
    """ get field names from a class containing mongoengine fields """
    res = {}
    for cls_ in reversed(cls.mro()):
        res.update({k: getattr(v, attr)
                    for k, v in vars(cls_).items()
                    if isinstance(v, BaseField) and hasattr(v, attr)})
    return res


def _get_field_choices(name, field):
    field_t = type(field)
    if issubclass(field_t, EmbeddedDocumentField):
        obj = field.document_type_obj
        n, choices = _get_field_choices(field.name, obj.field)
        return '%s__%s' % (name, n), choices
    elif issubclass(type(field), ListField):
        return name, field.field.choices
    return name, field.choices


def get_fields_with_attr(cls, attr, default=False):
    fields = []
    for field_name, field in cls._fields.items():
        if not getattr(field, attr, default):
            continue
        field_t = type(field)
        if issubclass(field_t, EmbeddedDocumentField):
            fields.extend((('%s__%s' % (field_name, name), choices)
                           for name, choices in get_fields_with_attr(field.document_type, attr, default)))
        elif issubclass(type(field), ListField):
            fields.append((field_name, field.field.choices))
        else:
            fields.append((field_name, field.choices))
    return fields


def get_items(cls):
    """ get key/value items from an enum-like class (members represent enumeration key/value) """

    res = {
        k: v
        for k, v in getmembers(cls)
        if not (k.startswith("_") or ismethod(v))
    }
    return res


def get_options(cls):
    """ get options from an enum-like class (members represent enumeration key/value) """
    return list(get_items(cls).values())


# return a dictionary of items which:
# 1. are in the call_data
# 2. are in the fields dictionary, and their value in the call_data matches the type in fields
# 3. are in the cls_fields
def parse_from_call(call_data, fields, cls_fields, discard_none_values=True):
    if not isinstance(fields, dict):
        # fields should be key=>type dict
        fields = {k: None for k in fields}
    fields = {k: v for k, v in fields.items() if k in cls_fields}
    res = {}
    with translate_errors_context('parsing call data'):
        for field, desc in fields.items():
            value = call_data.get(field)
            if value is None:
                if not discard_none_values and field in call_data:
                    # we'll keep the None value in case the field actually exists in the call data
                    res[field] = None
                continue
            if desc:
                if callable(desc):
                    desc(value)
                else:
                    if issubclass(desc, (list, tuple, dict)) and not isinstance(value, desc):
                        raise ParseCallError('expecting %s' % desc.__name__, field=field)
                    if issubclass(desc, Document) and not desc.objects(id=value).only('id'):
                        raise ParseCallError('expecting %s id' % desc.__name__, id=value, field=field)
            res[field] = value
        return res


def init_cls_from_base(cls, instance):
    return cls(**{k: v for k, v in instance.to_mongo(use_db_field=False).to_dict().items() if k[0] != '_'})


def get_company_or_none_constraint(company=None):
    return Q(company__in=(company, None, '')) | Q(company__exists=False)


def field_does_not_exist(field: str, empty_value=None, is_list=False) -> Q:
    """
    Creates a query object used for finding a field that doesn't exist, or has None or an empty value.
    :param field: Field name
    :param empty_value: The empty value to test for (None means no specific empty value will be used)
    :param is_list: Is this a list (array) field. In this case, instead of testing for an empty value,
                    the length of the array will be used (len==0 means empty)
    :return:
    """
    query = (Q(**{f"{field}__exists": False}) |
             Q(**{f"{field}__in": {empty_value, None}}))
    if is_list:
        query |= Q(**{f"{field}__size": 0})
    return query


def get_subkey(d, key_path, default=None):
    """ Get a key from a nested dictionary. kay_path is a '.' separated string of keys used to traverse
        the nested dictionary.
    """
    keys = key_path.split('.')
    for i, key in enumerate(keys):
        if not isinstance(d, dict):
            raise KeyError('Expecting a dict (%s)' % ('.'.join(keys[:i]) if i else 'bad input'))
        d = d.get(key)
        if key is None:
            return default
    return d


def id():
    return str(uuid4()).replace("-", "")


def hash_field_name(s):
    """ Hash field name into a unique safe string """
    return hashlib.md5(s.encode()).hexdigest()


def merge_dicts(*dicts):
    base = {}
    for dct in dicts:
        base.update(dct)
    return base


def filter_fields(cls, fields):
    """From the fields dictionary return only the fields that match cls fields"""
    return {key: fields[key] for key in fields if key in get_fields(cls)}
