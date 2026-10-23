// Compose-only fixture; never seed a production definition from this file.
const database = db.getSiblingDB(process.env.AUDIENCE_DATABASE_NAME || "ouros_audience");
const definitions = database.getCollection("audience_definitions");

definitions.createIndex({ key: 1 }, { unique: true, name: "uq_audience_definitions_key" });
definitions.insertOne({
  key: "high_water_consumption",
  version: "1",
  enabled: true,
  subject: "farm",
  member_id_space: "farm_owner_user_id",
  source: "postgres",
  metric: "normalized_water_consumption",
  period: "last_complete_month",
  baseline: "previous_consecutive_months",
  activation_pct: 25,
  resolution_pct: 15,
  min_baseline_samples: 3,
  max_observation_age_days: 45,
  unknown_policy: "preserve_previous_membership",
  first_run_push: false,
});
