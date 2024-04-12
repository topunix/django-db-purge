---

## Django Database Purge

The Django Database Purge management command is a tool for efficiently removing unwanted records from your Django project's database based on a specified retention policy. This command helps you keep your database clean and optimized by permanently deleting records that are no longer needed.

### Features:

- **Flexible Retention Policy**: Define your own retention policy to determine which records should be purged from the database.
- **Efficient Data Management**: Easily manage the size of your database by removing outdated or unnecessary records.
- **Customizable**: Adapt the command to suit your project's specific requirements and database structure.
- **Safe**: Built-in safeguards to prevent accidental data loss, ensuring that only the intended records are purged.

### How to Use:

```bash
pip install django-db-purge
```

Include dbpurge in your INSTALLED_APPS. Then, create your database purgers or file purgers in the admin interface.

Then, either periodically call the purge management command (e.g., via a system cronjob), or install and configure django-cron.


### Usage:

After installation, use the management command to purge records from your database according to your specified retention policy.

```bash
python manage.py db_purge
```

Include purge in your INSTALLED_APPS. Then, create your database purgers or file purgers in the admin interface.

Then, either periodically call the purge management command (e.g., via a system cronjob), or install and configure django-cron (add purge.cron to your CRON_CLASSES in your settings.py). The builtin CronJob class is set to run every 4 hours. You can change this by altering your settings.py and adding PURGE_CRON_RUN_AT_TIMES to an array of times you want to run the job at (e.g., ['1:00'] to run at 1am).

### Contributions:

Contributions are welcome! If you encounter any issues or have suggestions for improvements, please submit an issue or pull request on [GitHub](link_to_github_repo).

### License:

This project is licensed under the [MIT License](link_to_license_file).

---
