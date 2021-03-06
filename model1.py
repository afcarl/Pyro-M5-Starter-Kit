# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

"""
Top-down Model
==============

This script gives an example on how to use Pyro forecast module to backtest and
make a submission for M5 accuracy/uncertainty competition.

Using the top-down approach in [1], we first construct a model to predict
the aggregated sales across all items. Then we will distribute the aggregated
prediction to each product based on its total sales during the last 28 days.

The results are a little bit better than the best benchmark models from both accuracy
and uncertainty competition.

**References**

    1. Rob J Hyndman and George Athanasopoulos (2018), "Forecasting: Principles and Practice",
       (https://otexts.com/fpp2/top-down.html)
"""

import argparse
import os
import pickle

import numpy as np
import pyro
import pyro.distributions as dist
import torch
from pyro.contrib.forecast import ForecastingModel, Forecaster
from pyro.ops.tensor_utils import periodic_repeat

from evaluate import m5_backtest
from util import M5Data


RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
RESULTS = os.environ.get("PYRO_M5_RESULTS", RESULTS)
if not os.path.exists(RESULTS):
    os.makedirs(RESULTS)


# The model we are going to construct is a linear model in log scale
# with additive weekly seasonality.
class Model(ForecastingModel):
    def model(self, zero_data, covariates):
        assert zero_data.size(-1) == 1  # univariate
        duration = zero_data.size(-2)
        time, feature = covariates[..., 0], covariates[..., 1:]

        bias = pyro.sample("bias", dist.Normal(0, 10))
        # construct a linear trend; we know that the sales are increasing
        # through years, so a positive-support prior should be used here
        trend_coef = pyro.sample("trend", dist.LogNormal(-2, 1))
        trend = trend_coef * time
        # set prior of weights of the remaining covariates
        weight = pyro.sample("weight",
                             dist.Normal(0, 1).expand([feature.size(-1)]).to_event(1))
        regressor = (weight * feature).sum(-1)
        # encode the additive weekly seasonality
        with pyro.plate("day_of_week", 7, dim=-1):
            seasonal = pyro.sample("seasonal", dist.Normal(0, 5))
        seasonal = periodic_repeat(seasonal, duration, dim=-1)

        # make prediction
        prediction = bias + trend + seasonal + regressor
        # because Pyro forecasting framework is multivariate,
        # for univariate timeseries we need to make sure that
        # the last dimension is 1
        prediction = prediction.unsqueeze(-1)

        # Now, we will use heavy tail noise because the data has some outliers
        # (such as Christmas day)
        dof = pyro.sample("dof", dist.Uniform(1, 10))
        noise_scale = pyro.sample("noise_scale", dist.LogNormal(-2, 1))
        noise_dist = dist.StudentT(dof.unsqueeze(-1), 0, noise_scale.unsqueeze(-1))
        self.predict(noise_dist, prediction)


def main(args):
    # The M5Data class can load and provide helpful properties and methods
    # to manipulate the dataset. It is advised to take a look at its
    # definition in `util.py` file.
    m5 = M5Data()
    # get aggregated sales of all items from all Walmart stores
    data = m5.get_aggregated_sales(m5.aggregation_levels[0])[0].unsqueeze(-1)
    # apply log transform to scale down the data
    data = data.log()

    T0 = 0                   # begining
    T2 = data.size(-2) + 28  # end + submission-interval
    time = torch.arange(T0, float(T2), device="cpu") / 365
    covariates = torch.cat([
        time.unsqueeze(-1),
        # we will use dummy days of month (1, 2, ..., 31) as feature;
        # alternatively, we can use SNAP feature `m5.get_snap()[T0:T2]`
        m5.get_dummy_day_of_month()[T0:T2],
    ], dim=-1)

    if args.cuda:
        data = data.cuda()
        covariates = covariates.cuda()

    forecaster_options = {
        "learning_rate": args.learning_rate,
        "learning_rate_decay": args.learning_rate_decay,
        "clip_norm": args.clip_norm,
        "num_steps": args.num_steps,
        "log_every": args.log_every,
    }

    def transform(pred, truth):
        return pred.exp(), truth.exp()

    if args.submit:
        pyro.set_rng_seed(args.seed)
        forecaster = Forecaster(Model(), data, covariates[:-28], **forecaster_options)
        samples = forecaster(data, covariates, num_samples=1000).exp().squeeze(-1).cpu()
        pred = samples.mean(0)

        # we use top-down approach to distribute the aggregated forecast sales `pred`
        # for each items at the bottom level;
        # the proportion is calculated based on the proportion of total sales of each time
        # during the last 28 days (this follows M5 guide's benchmark models)
        sales_last28 = m5.get_aggregated_sales(m5.aggregation_levels[-1])[:, -28:]
        proportion = sales_last28.sum(-1) / sales_last28.sum()
        prediction = proportion.ger(pred)
        # make the accuracy submission
        m5.make_accuracy_submission(args.output_file, prediction)

        # Similarly, we also use top-down approach for uncertainty prediction
        non_agg_samples = samples.unsqueeze(1) * proportion.unsqueeze(-1)
        # Note that in the above code, we distributed the aggregated result to
        # each individual timeseries at the non-aggregated level. In other words,
        # we just scale down the aggregated predictions. The standard deviation
        # of the aggregated data is about ~7000. Hence, with 30490 non-aggregated
        # timeseries, in average, each of them will have a pretty small standard
        # deviation (about 0.2). Hence, quantile results will be pretty bad.
        # To remedy that issue, we will assume that the non-aggregated timeseries
        # has Poisson distribution and the top-down approach gives prediction of
        # the rate (also the mean) of Poisson distribution. In other words,
        # we need to draw Poisson samples from the non-aggregated prediction.
        non_agg_samples = torch.poisson(non_agg_samples)
        agg_samples = m5.aggregate_samples(non_agg_samples, *m5.aggregation_levels)
        # cast to numpy because pyro quantile implementation is memory hungry
        print("Calculate quantiles...")
        q = np.quantile(agg_samples.numpy(), m5.quantiles, axis=0)
        print("Make uncertainty submission...")
        filename, ext = os.path.splitext(args.output_file)
        m5.make_uncertainty_submission(filename + "_uncertainty" + ext, q, float_format='%.3f')
    else:
        # Here we do backtesting to verify if our generative model is a good candidate.
        # This is a popular validation method for timeseries forecasting.
        # You can use backtest to adjust your priors. Another helpful method to
        # adjust priors is to train a `forecaster` as above and inspect the parameters
        # using the code `forecaster.guide.median()`).

        # Calculate the smallest training window so that the total number of windows for
        # backtesting is the same as the one defined in `args.num_windows`.
        min_train_window = data.size(-2) - args.test_window - (args.num_windows - 1) * args.stride
        windows = m5_backtest(data, covariates[:-28], Model,
                              transform=transform,
                              min_train_window=min_train_window,
                              test_window=args.test_window,
                              stride=args.stride,
                              forecaster_options=forecaster_options,
                              seed=args.seed)

        # by default, the result will be saved in `results/model1.pkl` file
        with open(args.output_file, "wb") as f:
            pickle.dump(windows, f)

        # print out the mean and std of the weighted RMSSE and weighted SPL metrics
        for name in ["ws_rmse", "ws_pl"]:
            values = torch.tensor([w[name] for w in windows])
            print("{} = {:0.3g} +- {:0.2g}".format(name, values.mean(), values.std()))


if __name__ == "__main__":
    assert pyro.__version__ >= "1.3.0"
    parser = argparse.ArgumentParser(description="Univariate M5 daily forecasting")
    parser.add_argument("--num-windows", default=3, type=int)
    parser.add_argument("--test-window", default=28, type=int)
    parser.add_argument("-s", "--stride", default=35, type=int)
    parser.add_argument("-n", "--num-steps", default=1001, type=int)
    parser.add_argument("-lr", "--learning-rate", default=0.1, type=float)
    parser.add_argument("--learning-rate-decay", default=0.1, type=float)
    parser.add_argument("--clip-norm", default=10., type=float)
    parser.add_argument("--log-every", default=100, type=int)
    parser.add_argument("--seed", default=1, type=int)
    parser.add_argument("-o", "--output-file", default="", type=str)
    parser.add_argument("--submit", action="store_true", default=False)
    parser.add_argument("--cuda", action="store_true", default=False)
    args = parser.parse_args()

    if args.cuda and not torch.cuda.is_available():
        args.cuda = False

    if args.cuda:
        torch.set_default_tensor_type(torch.cuda.FloatTensor)

    if args.output_file == "":
        args.output_file = os.path.basename(__file__)[:-3] + (".csv" if args.submit else ".pkl")
    args.output_file = os.path.join(RESULTS, args.output_file)

    main(args)
