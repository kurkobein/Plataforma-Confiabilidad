from django import template

register = template.Library()


@register.filter
def attr(obj, field_name):
    value = getattr(obj, field_name)
    if value is None or value == '':
        return '-'
    return value


@register.filter
def field_label(field):
    return field.verbose_name.replace('_', ' ').title()


@register.filter
def get_item(obj, key):
    if isinstance(obj, dict):
        return obj.get(key, '')
    return ''