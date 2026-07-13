from django.db import models


class SampleRecord(models.Model):
    created_at = models.DateTimeField()
    label = models.CharField(max_length=64)
