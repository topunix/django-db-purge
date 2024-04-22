---

## Django Database Purge

The Django Database Purge management command is a tool for efficiently removing unwanted records from your Django project's database based on a specified retention policy. This command helps you keep your database clean and optimized by permanently deleting records that are no longer needed.

### Features:

- **Flexible Retention Policy**: Define your own retention policy to determine which records should be purged from the database.
- **Efficient Data Management**: Easily manage the size of your database by removing outdated or unnecessary records.
- **Customizable**: Adapt the command to suit your project's specific requirements and database structure.
- **Safe**: Built-in safeguards to prevent accidental data loss, ensuring that only the intended records are purged.

### How to Use:
1. Install django-db-purge by running:
```bash
pip install django-db-purge
```
2. Include 'dbpurge' in your INSTALLED_APPS settings. 
3. Locate the `db_purge.py` file in the `management/commands` directory of the Django dbpurge app.
4. Add your own values to the retention policies dictionary in the `db_purge.py` file, based on your requirements. Below is a guide on how to set up the retention policies:

    #### 1. `app_name`

    - **Description**: Name of the Django app containing the model.
    - **Example**: `my_django_app`

    #### 2. `model_name`

    - **Description**: Name of the Django model from which records will be deleted.
    - **Example**: `MyModel`

    #### 3. `time_based_column_name`

    - **Description**: Name of the column in the model that contains the timestamp or datetime field used for determining the age of records.
    - **Example**: `created_at`

    #### 4. `data_retention_num_seconds`

    - **Description**: Time duration in seconds for which records will be retained before deletion.
    - **Example**: `2592000` (for 30 days)

    #### Example:

    ```python
    retention_policies = [
        {
            'app_name': 'my_django_app',
            'model_name': 'MyModel',
            'time_based_column_name': 'created_at',
            'data_retention_num_seconds': 2592000,  # 30 days in seconds
        },
        # Add more retention policies as needed
    ]
    ```
5. Then, either periodically call the db_purge management command (e.g., via a system cronjob), or install and configure django-cron.

### Contributions:

Contributions are welcome! If you encounter any issues or have suggestions for improvements, please submit an issue or pull request on GitHub.

### License:

This project is licensed under the MIT License.

---
