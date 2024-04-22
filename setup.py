import os
from setuptools import find_packages, setup

# allow setup.py to be run from any path
os.chdir(os.path.normpath(os.path.join(os.path.abspath(__file__), os.pardir)))

# get README
with open('README.rst') as f:
    long_description = f.read()

setup(
    name='django-db-purge',
    version='0.2',
    packages=find_packages(),
    description='Clean up your Django database effortlessly with customizable record removal based on your retention policy',
    long_description_content_type="text/markdown",
    long_description = long_description,
    install_requires=['Django>=2'],
    url='https://github.com/topunix/django-db-purge',
    author='topunix',
    author_email='topunixguy@gmail.com',
    license='MIT',
    classifiers=[
        'Environment :: Web Environment',
        'Framework :: Django',
        'Framework :: Django :: 2.0',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Topic :: Internet :: WWW/HTTP',
    ],
)
