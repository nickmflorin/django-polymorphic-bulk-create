# django-polymorphic-bulk-create
Experimental Code for Bulk Create Behavior with Polymorphic Models

Please note that this is experimental code, and will likely not blindly work for all use cases.  I am thinking about productionizing this as a potential installable package, but I haven't had the time yet.

### Usage

The bulk create works by first doing a bulk create on the base polymorphic model, where the PKs are
defined explicitly before the database transaction so that they can be used to map to the bulk created
children models.  This is necessary because Django's bulk create will not return the fields auto generated
by the database.  These IDs are then used to map to the bulk creation of thte children instances.

The base instances and children instances are instantiated for the bulk create with only the fields
local to the model (i.e. the child model will not be instantiated with the parent model fields).  This
allows us to perform the bulk create on each model's table separately, without creating rows in the table
for both the parent and children models when bulk creating the children.

#### Managers

```python
from polymorphic.managers import PolymorphicManager
from whereever import BulkCreatePolymorphicQuerySet


class BaseQuerier(object):

    def active(self):
        return self.filter(active=True)

    def inactive(self):
        return self.filter(active=False)


class BaseQuery(BaseQuerier, BulkCreatePolymorphicQuerySet):
    pass


class BaseManager(BaseQuerier, PolymorphicManager):
    queryset_class = BaseQuery

    def get_queryset(self):
        return self.queryset_class(self.model)


class ChildManager(BaseManager):
    pass
```

#### Models

```python
from polymorphic.models import PolymorphicModel
from django.db import models

from .managers import BaseManager, ChildManager


class Base(PolymorphicModel):
    description = models.CharField(null=True, max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    active = models.BooleanField(default=True)
    
    objects = BaseManager()
    # Must expose the non-polymorphic manager on the base model.
    non_polymorphic = models.Manager()


class Child(Base):
    additional_description = models.CharField(null=True, max_length=128)
    created_by = models.ForeignKey(
        to='user.User',
        related_name='created_children',
        on_delete=models.SET_NULL,
        null=True
    )
    objects = ChildManager()
```

#### Example

```python
instances = [
    models.Child(description='Child 1', created_by=user),
    models.Child(description='Child 2', created_by=user),
    ...
]
created = models.Child.objects.bulk_create(instances,
    return_created_objects=True)
```
