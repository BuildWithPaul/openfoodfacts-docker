from setuptools import setup

setup(
    name="off-api",
    version="1.0.0",
    py_modules=["app", "import_off"],
    install_requires=[
        "fastapi>=0.110",
        "uvicorn[standard]>=0.29",
        "sqlalchemy>=2.0",
        "psycopg2-binary>=2.9",
        "httpx>=0.27",
        "orjson>=3.10",
    ],
)