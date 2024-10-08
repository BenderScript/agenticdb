from http import HTTPStatus
import unittest
import warnings
import random
from unittest import IsolatedAsyncioTestCase

import yaml
from fastapi.testclient import TestClient
from langserve import RemoteRunnable
from app.server import create_app

# Suppress DeprecationWarnings
warnings.filterwarnings("ignore", category=DeprecationWarning)


class TestChangeRatings(IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        cls.post_headers = {'Content-Type': 'application/x-yaml'}
        cls.get_headers = {'Accept': 'application/x-yaml'}

    def setUp(self):
        self.base_url = "http://127.0.0.1:8000"
        self.test_agent_yaml = """
metadata:
  name: financial-data-oracle
  namespace: sandbox
  description: |
    Retrieves financial price data for a variety of tickers and timeframes.
spec:
  type: agent
  lifecycle: experimental
  owner: buddy@example.com
  access_level: PRIVATE
  category: Natural Language
  url: https://api.example.com/financial-data-oracle
  parameters:
    type: object
    properties:
      symbol:
        type: string
        description: ticker symbol
      date:
        type: string
        description: A specific date in the format yyyy-mm-dd
      currency:
        type: string
        enum:
          - USD
          - JPY
        description: "the currency of the desired output value"
    required:
      - symbol
    additionalProperties: false
  output:
    type: float
    description: Output description for financial-data-oracle
"""
        self.ratings_yaml = f"""
        ratings:
          agent_id: placeholder_agent_id
          id: placeholder_some_id
          data:
            score: {random.randint(1, 5)}
        """

    def test_change_ratings(self):
        with TestClient(create_app()) as c:
            response = c.post("/agents", content=self.test_agent_yaml, headers=self.post_headers)
            self.assertEqual(HTTPStatus.OK, response.status_code)
            agents = list(yaml.safe_load_all(response.content))
            required_keys = {"metadata", "spec"}
            for agent in agents:
                self.assertTrue(required_keys.issubset(agent.keys()))
                self.assertIn("type", agent["spec"])
                self.assertEqual("agent", agent["spec"]["type"])
                # Update ratings
                ratings_dict = yaml.safe_load(self.ratings_yaml)
                ratings_dict["ratings"]["agent_id"] = agent["metadata"]["id"]
                ratings_dict["ratings"]["id"] = agent["metadata"]["ratings_id"]
                updated_ratings_yaml = yaml.dump(ratings_dict)
                # Post new ratings
                response = c.post("/ratings", content=updated_ratings_yaml, headers=self.post_headers)
                self.assertEqual(HTTPStatus.OK, response.status_code)
                response_json = response.json()
                self.assertEqual(ratings_dict["ratings"]["data"]["score"], response_json["ratings"]["data"]["score"])
                # round trip and get new ratings
                response = c.get("/ratings", params={"ratings_id": ratings_dict["ratings"]["id"]}, headers=self.get_headers)
                self.assertEqual(HTTPStatus.OK, response.status_code)
                response_json = response.json()
                self.assertEqual(ratings_dict["ratings"]["data"]["score"], response_json["data"]["score"])
                self.assertEqual(ratings_dict["ratings"]["id"], response_json["id"])
                print(response.content.decode("utf-8"))


if __name__ == '__main__':
    unittest.main()
