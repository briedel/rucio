# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Vincent Garonne, <vincent.garonne@cern.ch>, 2012

import sqlalchemy
import sqlalchemy.orm

from sqlalchemy.exc import IntegrityError

from rucio.common import exception
from rucio.db import models1 as models
from rucio.db.history import versioned_session
from rucio.db.session import get_session


session = get_session()


def add_rse(rse):
    """ Add a rse with the given location name.

    :param rse: the name of the new rse.
    """

    new_rse = models.RSE(rse=rse)
    try:
        new_rse.save(session=session)
    except IntegrityError:
        session.rollback()
        raise exception.Duplicate('RSE \'%(rse)s\' already exists!' % locals())

    session.commit()

    return new_rse.id


def rse_exists(rse):
    """ Checks to see if RSE exists. This procedure does not check its status.

    :param rse: Name of the rse.
    :returns: True if found, otherwise false.
    """

    return True if session.query(models.RSE).filter_by(rse=rse).first() else False


def del_rse(rse):
    """ Disable a rse with the given rse name.

    :param rse: the rse name.
    """

    try:
        old_rse = session.query(models.RSE).filter_by(rse=rse).one()
    except sqlalchemy.orm.exc.NoResultFound:
        raise exception.RSENotFound('RSE \'%s\' cannot be found' % rse)

    old_rse.delete(session)
    session.commit()


def get_rse(rse):
    """Get a RSE or raise if it does not exist."""
    try:
        query = session.query(models.RSE).filter_by(rse=rse)
        location = query.one()
    except sqlalchemy.orm.exc.NoResultFound:
        raise exception.RSENotFound('RSE \'%s\' cannot be found' % rse)
    return location


def list_rses():
    """ Returns a list of all RSE names.

    returns: a list of all RSE names.
    """

    rse_list = []

    for rse in session.query(models.RSE).order_by(models.RSE.rse):
        rse_list.append(rse.rse)

    return rse_list


def get_rse_tag(tag):
    """Get a rse tag or raise if it does not exist."""
    try:
        query = session.query(models.RSETag).filter_by(tag=tag)
        rse = query.one()
        return rse
    except sqlalchemy.orm.exc.NoResultFound:
        raise exception.RSETagNotFound('RSE Tag \'%s\' cannot be found' % tag)


def add_tag(tag, description=None):
    """ Add a RSE tag.

    :param tag: The RSE tag  name.
    :param description: The description of the RSE tag.
    """
    try:
        new_rse_tag = models.RSETag(tag=tag, description=description)
        new_rse_tag.save(session=session)
    except IntegrityError:
        session.rollback()
    return new_rse_tag


def add_rse_attribute(rse, key, value):
    """ Adds a RSE attribute.

    :param rse: the rse name.
    :param key: the key name.
    :param value: the value name.
    :param issuer: The issuer account.

    returns: True is successfull
    """
    try:
        # Check location
        l = get_rse(rse=rse)

        query = session.query(models.RSEAttribute).filter(models.RSEAttribute.key == key).filter(models.RSEAttribute.value == value)
        if not query.count():
            new_attr = models.RSEAttribute(key=key, value=value)
            new_attr.save(session=session)

        try:
            new_rse_attr = models.RSEAttrAssociation(rse_id=l.id, key=key, value=value, deleted=False)
            new_rse_attr = session.merge(new_rse_attr)
            new_rse_attr.save(session=session)
            session.commit()
        except IntegrityError, e:
                raise exception.Duplicate("RSE attribute '%(key)s-%(value)s\' for RSE '%(rse)s' already exists!" % locals())
    finally:
        session.rollback()


def del_rse_attribute(rse, key):
    """
    Delete a RSE attribute.

    :param rse: the name of the rse.
    :param key: the attribute key.

    :return: True if RSE attribute was deleted successfully else False.
    """
    l = get_rse(rse=rse)
    query = session.query(models.RSEAttrAssociation).filter_by(rse_id=l.id, deleted=False).filter(models.RSEAttrAssociation.key == key)
    try:
        rse_attr = query.one()
        rse_attr.delete(session=session)
        session.commit()
    except:
        pass


def list_rse_attributes(rse):
    """ List RSE attributes for a RSE.

    :param rse: the rse name.

    :returns: A dictionary with RSE attributes for a RSE.
    """
    rse_attrs = {}
    l = get_rse(rse=rse)

    query = session.query(models.RSEAttrAssociation).filter_by(rse_id=l.id, deleted=False)
    for attr in query:
        rse_attrs[attr.key] = attr.value
    return rse_attrs


def get_rses(filters=None):
    """ Gets the list of RSEs

    :param filters: dictionary of attributes by which the results should be filtered.

    returns: List of locations.
    """
    query = session.query(models.RSETagAssociation).\
        join(models.RSE, models.RSE.id == models.RSETagAssociation.rse_id).\
        join(models.RSETag, models.RSETag.id == models.RSETagAssociation.rse_tag_id)

    if filters:
        for (k, v) in filters.items():
            if hasattr(models.RSE, k):
                query = query.filter(getattr(models.RSE, k) == v)
            if hasattr(models.RSETag, k):
                query = query.filter(getattr(models.RSETag, k) == v)
    tags = list()
    for tag in query:
        tags.append({'rse': tag.rse.rse, 'tag': tag.tag.tag})
    return tags


def set_rse_usage(rse, source, total, free):
    """ Set RSE usage information.

    :param rse: the location name.
    :param source: the information source, e.g. srm.
    :param total: the total space in bytes.
    :param free: the free in bytes.
    :returns: True if successfull, otherwise false.
    """

    rse = session.query(models.RSE).filter_by(rse=rse).one()

    rse_usage = models.RSEUsage(rse_id=rse.id, source=source, total=total, free=free)

    versioned_session(session)
    merged_rse_usage = session.merge(rse_usage)
    merged_rse_usage.save(session=session)

    session.commit()
    return True


def get_rse_usage(rse, filters=None):
    """ get rse usage information.

    :param rse: the rse name.
    :param filters: dictionary of attributes by which the results should be filtered

    :returns: True if successfull, otherwise false.
    """

    query = session.query(models.RSEUsage).\
        join(models.RSE, models.RSE.id == models.RSEUsage.rse_id).\
        filter_by(rse=rse)

    if filters:
        for (k, v) in filters.items():
            if hasattr(models.RSEUsage, k):
                query = query.filter(getattr(models.RSEUsage, k) == v)

    result = list()
    for usage in query:
        result.append({'rse': usage.rse.rse, 'source': usage.source,
                       'total': usage.total, 'free': usage.free,
                       'updated_at': usage.updated_at})
    return result


def get_rse_usage_history(rse, filters=None):
    """ get location usage history information.

    :param location: The location name.
    :param filters: dictionary of attributes by which the results should be filtered.

    :returns:  list of locations.
    """
    result = list()
    query = session.query(models.LocationUsage.__history_mapper__.class_)
    for usage in query:
        result.append({'location': usage.location.location, 'source': usage.source, usage.name: usage.value, 'updated_at': usage.updated_at})


def add_file_replica(rse, scope, name, size, checksum, issuer):
    """ Add File replica.

    :param rse: the rse name.
    :param scope: the tag name.
    :param name: The file name.
    :param size: the size of the file.
    :param checksum: the checksum of the file.
    :param issuer: The issuer account.

    :returns: True is successfull.
    """
    new_data_id = models.DataIdentifier(scope=scope, name=name, owner=issuer, type=models.DataIdType.FILE)
    new_file = models.File(scope=scope, name=name, owner=issuer, size=size, checksum=checksum)
    replica_rse = get_rse(rse=rse)
    new_replica = models.RSEFileAssociation(rse_id=replica_rse.id, scope=scope, name=name, size=size, checksum=checksum)
    # Add optional pfn
    try:
        new_data_id.save(session=session)
        new_file.save(session=session)
    except IntegrityError, e:
        'columns scope, name are not unique'
        # needs to parse the exception string
        print e
        session.rollback()

    try:
        new_replica.save(session=session)
    except IntegrityError, e:
        session.rollback()
        raise exception.Duplicate("File replica '%(scope)s:%(name)s-%(rse)s' already exists!" % locals())

    session.commit()
