"""One-off import of the customers we recieve from the old system."""
import csv


def load_customers(path="data/legacy_customers.csv"):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))
