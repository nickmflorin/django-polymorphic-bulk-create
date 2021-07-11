from polymorphic.models import PolymorphicModel
from polymorphic.query import PolymorphicQuerySet

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import models, connections, connection, transaction
from django.utils.functional import partition


def reset_id_sequence(model_cls):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT setval('{sequence_table}', (SELECT MAX(id) from \"{db_table}\"));".format(  # noqa
                sequence_table='%s_id_seq' % model_cls._meta.db_table,
                db_table=model_cls._meta.db_table
            )
        )


class PrePKBulkCreateQuerySet(models.QuerySet):
    def bulk_create(self, instances, batch_size=None, ignore_conflicts=False,
            predetermine_pks=False):
        if predetermine_pks is False:
            return super().bulk_create(instances,
                batch_size=batch_size, ignore_conflicts=ignore_conflicts)
        try:
            max_id = int(self.model.objects.latest('pk').pk)
        except self.model.DoesNotExist:
            max_id = 0

        for i, instance in enumerate(instances):
            setattr(instance, 'pk', max_id + i + 1)
        return super().bulk_create(instances,
                batch_size=batch_size, ignore_conflicts=ignore_conflicts)


class BulkCreatePolymorphicQuerySet(PolymorphicQuerySet):
    """
    Extension of :obj:`polymorphic.query.PolymorphicQuerySet` that incorporates
    Django's bulk create behavior.

    Django's bulk create behavior does not support multi-table inheritance,
    which is problematic for Polymorphic models.  This implementation aims to
    solve that problem.

    Note that the Django mechanics that this class taps into are highly
    complicated, and this implementation is not fool-proof.  It is likely
    that we will have to adjust it as time progresses in order to support
    various other types of fields or circumstances that it currently does
    not support.
    """
    @property
    def polymorphic_base(self):
        assert len(self.model.__bases__) == 1, \
            "Models that inherit from multiple tables at the same time are " \
            "not supported."
        return self.model.__bases__[0]

    @property
    def polymorphic_base_model_fields(self):
        fields = []
        # Note the use of `local_fields` - this deviates very much from the
        # way that Django traditionally generates the SQL Insert statements
        # from a model, usually looking at the concrete_fields.
        for field in self.polymorphic_base._meta.local_fields:
            if not isinstance(field, models.fields.AutoField) \
                    and field.name != 'polymorphic_ctype':
                fields.append(field)
        return fields

    @property
    def polymorphic_child_model_fields(self):
        fields = []
        # Note the use of `local_fields` - this deviates very much from the
        # way that Django traditionally generates the SQL Insert statements
        # from a model, usually looking at the concrete_fields.
        for field in self.model._meta.local_fields:
            # This field, is an auto-field on the child polymorphic model that
            # points to the specific ID of the parent polymorphic model.
            if isinstance(field, models.fields.related.OneToOneField) \
                    and field.name.endswith('_ptr'):
                continue
            # There shouldn't be an AutoField PK on the child model, but just
            # in case we will leave them out here.
            if not isinstance(field, models.fields.AutoField):
                fields.append(field)
        return fields

    @property
    def polymorphic_child_pointer_field(self):
        """
        Every Polymorphic child model has an auto field that is suffixed with
        `_ptr` that points to the specific ID of the base Polymorphic model
        it is associated with.
        """
        for field in self.model._meta.local_fields:
            if isinstance(field, models.fields.related.OneToOneField) \
                    and field.name.endswith('_ptr'):
                return field
        raise Exception(
            "Could not determine Polymorphic pointer field for %s model."
            % self.model.__name__
        )

    @property
    def auto_pk_field(self):
        for field in self.polymorphic_base._meta.local_fields:
            if isinstance(field, models.fields.AutoField) \
                    and getattr(field, 'primary_key', None) is True:
                return field
        raise Exception(
            "Could not determine auto incrementing primary key field for "
            "%s model." % self.model.__name__
        )

    def recreate_polymorphic_base(self, instance, pk):
        """
        Reinstantiates the Polymorphic base model with only the fields local
        to the Polymorphic base model.

        The first step to the bulk creation is to bulk create all of the
        base models, and then bulk create the child models with each one
        tied to the appropriately created base model.  In order to do this,
        since bulk create operations do not return the IDs of the created
        objects, we need to explicitly set the IDs of the base models ahead
        of time, so they can be used to map to the children models when they
        are bulk created.
        """
        kwargs = {}
        for field in self.polymorphic_base_model_fields:
            try:
                kwargs[field.name] = getattr(instance, field.name)
            except ObjectDoesNotExist:
                # This can happen if the related object is expected but not
                # set on the model before it is saved.  Instead of raising an
                # error here, we want to let Django raise the error for the
                # field being missing when the bulk create is performed.
                # Example: This will get raised if bulk creating Budget(s) and
                # not specifying a `created_by` user for any given Budget.
                pass
        kwargs.update(
            polymorphic_ctype=ContentType.objects.get_for_model(self.model),
            pk=pk
        )
        return self.polymorphic_base(**kwargs)

    def recreate_polymorphic_child(self, instance, base):
        kwargs = {}
        for field in self.polymorphic_child_model_fields:
            try:
                kwargs[field.name] = getattr(instance, field.name)
            except ObjectDoesNotExist:
                pass
        pointer_field = self.polymorphic_child_pointer_field
        kwargs['%s_id' % pointer_field.name] = base.pk
        instantiated_instance = self.model(**kwargs)
        # NOTE: Django does not know that this model already has a polymorphic
        # base, so Django will auto-create an ID for it.  This means we have
        # to delete that attribute, otherwise the write will fail.
        auto_pk_field = self.auto_pk_field
        delattr(instantiated_instance, auto_pk_field.name)
        return instantiated_instance

    def bulk_create(self, instances, batch_size=None, ignore_conflicts=False,
            return_created_objects=False, refresh_from_db=False):

        assert batch_size is None or batch_size > 0

        if not instances:
            return instances

        # Make sure that the current model is in fact Polymorphic.
        assert issubclass(self.polymorphic_base, PolymorphicModel), \
            "The model %s is not polymorphic." % self.model.__name__

        # Make sure that the Polymorphic base model has a non-polymorphic
        # :obj:`django.db.models.Manager` that can be used to create objects
        # directly.
        assert hasattr(self.polymorphic_base, "non_polymorphic") \
            and isinstance(
                getattr(self.polymorphic_base, "non_polymorphic"),
                models.Manager), \
            "The polymorphic base model %s must define a " \
            "`non_polymorphic` manager." % self.polymorphic_base.__name__

        # Make sure that the instances that we are bulk creating are all of
        # the same model type pertaining to this queryset.
        assert len(set([type(inst) for inst in instances])) == 1 \
            and all([type(a) is self.model for a in instances]), \
            "All instances being bulk created must be of type %s." \
            % type(self.model)

        # Usually, Django does support supplying objects with the PK already set
        # to bulk create - but we cannot do that, because of the need to set
        # the PKs in a predictable way in the parent models.
        assert not any([inst.pk is not None for inst in instances]), \
            "The instances supplied to the bulk create operation cannot have " \
            "PK values specified already."

        base_instances = []
        try:
            max_id = int(self.polymorphic_base.objects.latest('pk').pk)
        except self.polymorphic_base.DoesNotExist:
            max_id = 0

        # Reinstantiate the polymorphic base models with only the fields
        # local to the base model, keeping track of the primary keys that are
        # used for each polymorphic base model.
        for i, instance in enumerate(instances):
            # Each base instance must be created with an explicit PK so we can
            # use it to map to the appropriate child instance.
            base_instances.append(self.recreate_polymorphic_base(
                instance=instance,
                pk=max_id + i + 1
            ))

        with transaction.atomic(using=self.db, savepoint=False):
            # The created polymorphic base models that are not associated with
            # their children yet.
            created_polymorphic_bases = self.polymorphic_base \
                .non_polymorphic.bulk_create(base_instances)

            child_instances = []
            for i, instance in enumerate(instances):
                child_instances.append(self.recreate_polymorphic_child(
                    instance=instance,
                    base=created_polymorphic_bases[i]
                ))
            # Since the child model is not a base model, Django's non-polymorphic
            # bulk-create will not work.  We have to use our tweaked form of
            # it for the children.
            created_children = self._bulk_create(
                instances=child_instances,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts
            )

            # NOTE: Since we are manually setting the the IDs, PostGres does
            # not detect that the ID sequence needs to be automatically updated.
            # Therefore, we need to do this ourselves.  This will not work for
            # our SQLite test database though.
            db_name = 'default'
            if child_instances[0]._state.db is not None:
                db_name = child_instances[0]._state.db

            # This might also not work for MySQL but we don't use that ever.
            db_backend = settings.DATABASES[db_name]['ENGINE'].split('.')[-1]
            if db_backend != 'sqlite3':
                reset_id_sequence(self.polymorphic_base)

            # Note that while the created children are fully represented with
            # all fields (both base and child) in the database, the children
            # returned from the bulk-create operation will not have their
            # base fields populated.  To do this, we need to either manually
            # set them on the instances, or refresh the children from the DB
            # (which is a large query).  So we only do this if we are
            # intentionally told to do so.
            if return_created_objects:
                if refresh_from_db:
                    [obj.refresh_from_db() for obj in created_children]
                    return created_children
                for child in created_children:
                    parent = [
                        p for p in created_polymorphic_bases
                        # if p.pk == getattr(child, self.polymorphic_child_pointer_field.name)  # noqa
                        if p.pk == child.pk  # I think this is safe.
                    ][0]
                    for field in self.polymorphic_base._meta.local_fields:
                        if not isinstance(field, models.fields.AutoField):
                            setattr(child, field.name,
                                    getattr(parent, field.name))
                return created_children

    def _prepare_for_bulk_create(self, objs):
        # Direct copy of Django's version - not currently called, as it causes
        # the functionality to not work, but we might need to tailor it in the
        # future.
        for obj in objs:
            if obj.pk is None:
                # Populate new PK values.
                obj.pk = obj._meta.pk.get_pk_value_on_save(obj)
            obj._prepare_related_fields_for_save(operation_name='bulk_create')

    def _bulk_create(self, instances, batch_size=None, ignore_conflicts=False):
        """
        Adapted functionality of Django's default :obj:`django.db.models.QuerySet`  # noqa
        that is tweaked to work for the Polymorphic children models.
        """
        assert batch_size is None or batch_size > 0

        if not instances:
            return instances

        self._for_write = True
        connection = connections[self.db]
        opts = self.model._meta

        # NOTE: This is modified from Django's original version to just use the
        # model's local fields.
        fields = opts.local_fields

        objs = list(instances)

        # In order to get this to work, we have to comment out this call.  I'm
        # not yet sure if it will affect things, but the missing behavior/call
        # to obj._prepare_related_fields_for_save may cause problems.
        # self._prepare_for_bulk_create(objs)

        with transaction.atomic(using=self.db, savepoint=False):
            objs_with_pk, objs_without_pk = partition(
                lambda o: o.pk is None, objs)
            if objs_with_pk:
                returned_columns = self._batched_insert(
                    objs_with_pk,
                    fields,
                    batch_size,
                    ignore_conflicts=ignore_conflicts,
                )
                for obj_with_pk, results in zip(objs_with_pk, returned_columns):
                    for result, field in zip(results, opts.db_returning_fields):
                        if field != opts.pk:
                            setattr(obj_with_pk, field.attname, result)

                for obj_with_pk in objs_with_pk:
                    obj_with_pk._state.adding = False
                    obj_with_pk._state.db = self.db

            if objs_without_pk:
                fields = [
                    f for f in fields
                    if not isinstance(f, models.fields.AutoField)
                ]
                returned_columns = self._batched_insert(
                    objs_without_pk,
                    fields,
                    batch_size,
                    ignore_conflicts=ignore_conflicts,
                )
                if connection.features.can_return_rows_from_bulk_insert \
                        and not ignore_conflicts:
                    assert len(returned_columns) == len(objs_without_pk)

                for obj_without_pk, results in zip(objs_without_pk, returned_columns):  # noqa
                    for result, field in zip(results, opts.db_returning_fields):
                        setattr(obj_without_pk, field.attname, result)

                    obj_without_pk._state.adding = False
                    obj_without_pk._state.db = self.db

        return objs
