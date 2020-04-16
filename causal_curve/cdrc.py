"""
Defines the causal dose-response curve class (CDRC)
"""

import contextlib
import io
from pprint import pprint
import pdb

import numpy as np
import pandas as pd
from pandas.api.types import is_float_dtype, is_numeric_dtype
from pygam import LinearGAM, s
from scipy.stats import gamma, norm
import statsmodels.api as sm
from statsmodels.genmod.families.links import inverse_power as Inverse_Power

from causal_curve.core import Core



class CDRC(Core):
    """
    Causal Dose-Response Curve model

    In a multi-stage approach, this computes the generalized propensity score (GPS) function,
    and uses this in a generalized additive model (GAM) to correct treatment prediction of
    the outcome variable. Assumes continuous treatment and outcome variable.

    WARNINGS:

        * This algorithm assumes you've already performed the necessary transformations to
        categorical covariates (i.e. these variables are already one-hot encoded and
        one of the categories is excluded for each set of dummy variables).

        * Please take care to ensure that the "ignorability" assumption is met (i.e.
        all strong confounders are captured in your covariates and there is no
        informative censoring), otherwise your results will be biased, sometimes strongly so.

    Parameters
    ----------

    gps_family: str, optional (default =  None)
        Accepts one of the following values: 'normal', 'lognormal', 'gamma', and None.
        Is used to determine the family of the glm used to model the GPS function.
        Look at the distribution of your treatment variable to determine which family
        is more appropriate. If no value is provided for this parameter, the algorithm
        will use the best-fitting family type.

    treatment_grid_num: int, optional (default = 100)
        Takes the treatment, and creates an equally-spaced grid across its values. This is used
        to estimate the final causal dose-response curve. Higher value here means the
        final curve will be smoother, but also increases computation time. Default value is 100,
        and this is usually a reasonable number.

    spline_order: int, optional (default = 3)
        Order of the splines to use fitting the final GAM. Must be integer >= 1. Default value
        is 3 for cubic splines.

    n_splines: int, optional (default = 30)
        Number of splines to use for the treatment and GPS in the final GAM. Must be integer >= 2.
        Must be non-negative. Default value is 30.

    lambda_: float, optional (default = 0.5)
        Strength of smoothing penalty. Must be a positive float. Larger values enforce
        stronger smoothing. Default value is 0.5.

    max_iter: int, optional (default = 100)
        Maximum number of iterations allowed for the maximum likelihood algo to converge.
        Default value is 100.

    verbose: bool, optional (default = False)
        Determines whether the user will get. Default value is False.

    Attributes
    ----------

    grid_values: array of shape (treatment_grid_num, )
        The gridded values of the treatment variable. Equally spaced.

    best_gps_family: str
        If no gps_family is specified and the algorithm chooses the best glm family, this is
        the name of the family that was chosen.

    gps_deviance: float
        The GPS model deviance

    gps: array of shape (number of observations, )
        The calculated GPS for each observation

    gam_results: `pygam.LinearGAM` class
        trained model of `LinearGAM` class, from pyGAM library


    Methods
    ----------
    fit: (self, T, X, y)
        Fits the causal dose-response model

    calculate_CDRC: (self, ci)
        Calculates the CDRC (and confidence interval) from trained model

    print_gam_summary: (self)
        Prints pyGAM text summary of GAM predicting outcome from the treatment and the GPS.


    References
    ----------

    Galagate, D. Causal Inference with a Continuous Treatment and Outcome: Alternative
    Estimators for Parametric Dose-Response function with Applications. PhD thesis, 2016.

    Moodie E and Stephens DA. Estimation of dose–response functions for
    longitudinal data using the generalised propensity score. In: Statistical Methods in
    Medical Research 21(2), 2010, pp.149–166.

    Hirano K and Imbens GW. The propensity score with continuous treatments.
    In: Gelman A and Meng XL (eds) Applied bayesian modeling and causal inference
    from incomplete-data perspectives. Oxford, UK: Wiley, 2004, pp.73–84.

    """

    def __init__(self, gps_family = None, treatment_grid_num = 100, spline_order = 3, n_splines = 30, lambda_ = 0.5, max_iter = 100, verbose = False):

        self.gps_family = gps_family
        self.treatment_grid_num = treatment_grid_num
        self.spline_order = spline_order
        self.n_splines = n_splines
        self.lambda_ = lambda_
        self.max_iter = max_iter
        self.verbose = verbose

        # Validate the params
        self._validate_init_params()

        if self.verbose:
            print("Using the following params for CDRC:")
            pprint(self.get_params(), indent=4)


    def _validate_init_params(self):
        """
        Checks that the params used when instantiating CDRC are formatted correctly
        """
        # Checks for gps_family param
        if not isinstance(self.gps_family, (str, type(None))):
            raise TypeError(f"gps_family parameter must be a string or None, but found type {type(self.gps_family)}")

        if ((isinstance(self.gps_family, str)) and (self.gps_family not in ['normal', 'lognormal', 'gamma'])):
            raise ValueError(f"gps_family parameter must take on values of 'normal', 'lognormal', or 'gamma', but found {self.gps_family}")

        # Checks for treatment_grid_num
        if not isinstance(self.treatment_grid_num, int):
            raise TypeError(f"treatment_grid_num parameter must be an integer, but found type {type(self.treatment_grid_num)}")

        if (isinstance(self.treatment_grid_num, int)) and self.treatment_grid_num < 10:
            raise ValueError(f"treatment_grid_num parameter should be >= 10 so your final curve has enough resolution, but found value {self.treatment_grid_num}")

        if (isinstance(self.treatment_grid_num, int)) and self.treatment_grid_num >= 1000:
            raise ValueError(f"treatment_grid_num parameter is too high!")

        # Checks for spline_order
        if not isinstance(self.spline_order, int):
            raise TypeError(f"spline_order parameter must be an integer, but found type {type(self.spline_order)}")

        if (isinstance(self.spline_order, int)) and self.spline_order < 1:
            raise ValueError(f"spline_order parameter should be >= 1, but found {self.spline_order}")

        if (isinstance(self.spline_order, int)) and self.spline_order >= 30:
            raise ValueError(f"spline_order parameter is too high!")

        # Checks for n_splines
        if not isinstance(self.n_splines, int):
            raise TypeError(f"n_splines parameter must be an integer, but found type {type(self.n_splines)}")

        if (isinstance(self.n_splines, int)) and self.n_splines < 2:
            raise ValueError(f"n_splines parameter should be >= 2, but found {self.n_splines}")

        if (isinstance(self.n_splines, int)) and self.n_splines >= 100:
            raise ValueError(f"n_splines parameter is too high!")

        # Checks for lambda_
        if not isinstance(self.lambda_, float):
            raise TypeError(f"lambda_ parameter must be an float, but found type {type(self.lambda_)}")

        if (isinstance(self.lambda_, float)) and self.lambda_ <= 0:
            raise ValueError(f"lambda_ parameter should be >= 2, but found {self.lambda_}")

        if (isinstance(self.lambda_, float)) and self.lambda_ >= 1000:
            raise ValueError(f"lambda_ parameter is too high!")

        # Checks for max_iter
        if not isinstance(self.max_iter, int):
            raise TypeError(f"max_iter parameter must be an int, but found type {type(self.max_iter)}")

        if (isinstance(self.max_iter, int)) and self.max_iter <= 10:
            raise ValueError(f"max_iter parameter is too low! Results won't be reliable!")

        if (isinstance(self.max_iter, int)) and self.max_iter >= 1e6:
            raise ValueError(f"max_iter parameter is unnecessarily high!")

        # Checks for verbose
        if not isinstance(self.verbose, bool):
            raise TypeError(f"verbose parameter must be a boolean type, but found type {type(self.verbose)}")


    def _validate_fit_data(self):
        """
        Verifies that T, X, and y are formatted the right way
        """
        # Checks for T column
        if not is_float_dtype(self.T):
            raise TypeError(f"Treatment data must be of type float")

        # Make sure all X columns are float or int
        for column in self.X:
            if not is_numeric_dtype(self.X[column]):
                raise TypeError(f"All covariate (X) columns must be int or float type (i.e. must be numeric)")

        # Checks for Y column
        if not is_float_dtype(self.y):
            raise TypeError(f"Outcome data must be of type float")


    def _grid_values(self):
        """
        Produces initial grid values for the treatment variable
        """
        return np.quantile(self.T, q = np.linspace(start = 0, stop = 1, num = self.treatment_grid_num))


    def fit(self, T, X, y):
        """
        Fits the causal dose-response model. For now, this only accepts pandas format.

        Parameters
        ----------
        T : array-like, shape (n_samples,)
            A continuous treatment variable
        X : array-like, shape (n_samples, m_features)
            Covariates, where n_samples is the number of samples
            and m_features is the number of features
        y : array-like, shape (n_samples,)
            Outcome variable

        Returns
        ----------
        None

        """
        self.T = T
        self.X = X
        self.y = y

        # Validate this input data
        self._validate_fit_data()

        # Create grid_values
        self.grid_values = self._grid_values()

        # Estimating the GPS
        self.best_gps_family = self.gps_family

        # If no family specified, pick the best family
        if self.gps_family == None:
            if self.verbose:
                print(f"Fitting several GPS models and picking the best fitting one...")

            self.best_gps_family, self.gps_function, self.gps_deviance = self._find_best_gps_model()

            if self.verbose:
                print(f"Best fitting model was {self.best_gps_family}, which produced a deviance of {self.gps_deviance}")

        # Otherwise, go with the what the user provided...
        else:
            if self.verbose:
                print(f"Fitting GPS model of family '{self.best_gps_family}'...")

            if self.best_gps_family == 'normal':
                self.gps_function, self.gps_deviance = self._create_normal_gps_function()
            elif self.best_gps_family == 'lognormal':
                self.gps_function, self.gps_deviance = self._create_lognormal_gps_function()
            elif self.best_gps_family == 'gamma':
                self.gps_function, self.gps_deviance = self._create_gamma_gps_function()

        # Estimate the GPS
        if self.verbose:
            print(f"Saving GPS values...")

        self.gps = self.gps_function(self.T)

        # Create GAM that predicts outcome from the treatment and GPS
        if self.verbose:
            print(f"Fitting GAM using treatment and GPS...")

        # Save model results
        self.gam_results = self._fit_gam()

        f = io.StringIO()
        with contextlib.redirect_stdout(f):
            self.gam_results.summary()

        self._gam_summary_str = f.getvalue()

        if self.verbose:
            print(f"Calculating CDRC estimates for each treatment grid value...")

        # Loop over all grid values (`treatment_grid_num` in total)
        # and give GPS loading for each observation in the dataset
        self.gps_at_grid = self._gps_values_at_grid()


    def calculate_CDRC(self, ci = 0.95):
        """
        Using the results of the fitted model, this generates a point estimate for the CDRC
        at each of the values of the treatment grid. Connecting these estimates will produce
        the overall estimated CDRC. Percentile bootstrap confidence intervals are produced as well.

        Parameters
        ----------
        ci: float, bounded (0, 1.0).
            The desired confidence interval to produce. Default value is 0.95, corresponding
            to 95% confidence intervals.

        Returns
        ----------
        Pandas dataframe of treatment grid values, the CDRC point estimate at that value,
        and the associated lower and upper confidence interval bounds at that point.

        """
        self._validate_calculate_CDRC_params(ci)

        # Create CDRC predictions from trained GAM
        self._cdrc_preds = self._cdrc_predictions(ci)

        # For each column of _cdrc_preds, calculate the mean and confidence interval bounds
        results = []

        for i in range(0, self.treatment_grid_num):
            temp_grid_value = self.grid_values[i]
            temp_point_estimate = self._cdrc_preds[:,i,0].mean()
            temp_lower_bound = self._cdrc_preds[:,i,1].mean()
            temp_upper_bound = self._cdrc_preds[:,i,2].mean()
            results.append([temp_grid_value, temp_point_estimate, temp_lower_bound, temp_upper_bound])

        return pd.DataFrame(results, columns = ['Treatment', 'CDRC', 'Lower_CI', 'Upper_CI'])


    def _validate_calculate_CDRC_params(self, ci):
        """
        Validates the parameters given to `calculate_CDRC`
        """

        if not isinstance(ci, float):
            raise TypeError(f"`ci` parameter must be an float, but found type {type(ci)}")

        if (isinstance(ci, float) and ((ci <= 0) or (ci >= 1.0))):
            raise ValueError("`ci` parameter should be between (0, 1)")


    def _cdrc_predictions(self, ci):
        """
        Returns the predictions of CDRC for each value of the treatment grid.
        Essentially, we're making predictions using the original treatment and gps_at_grid
        """

        # To keep track of cdrc predictions, we create an empty 3d array of shape
        # (n_samples, treatment_grid_num, 3). The last dimension is of length 3 because
        # we are going to keep track of the point estimate of the prediction, as well as
        # the lower and upper bounds of the prediction interval
        cdrc_preds = np.zeros((len(self.T), self.treatment_grid_num, 3), dtype=float)

        # Loop through each of the grid values, predict point estimate and get prediction interval
        for i in range(0, self.treatment_grid_num):
            temp_T = np.repeat(self.grid_values[i], repeats = len(self.T))
            temp_gps = self.gps_at_grid[:,i]
            temp_cdrc_preds = self.gam_results.predict(np.column_stack((temp_T, temp_gps)))
            temp_cdrc_interval = self.gam_results.prediction_intervals(np.column_stack((temp_T, temp_gps)), width=ci)
            temp_cdrc_lower_bound = temp_cdrc_interval[:,0]
            temp_cdrc_upper_bound = temp_cdrc_interval[:,1]
            cdrc_preds[:,i,0] = temp_cdrc_preds
            cdrc_preds[:,i,1] = temp_cdrc_lower_bound
            cdrc_preds[:,i,2] = temp_cdrc_upper_bound

        return np.round(cdrc_preds, 3)


    def _gps_values_at_grid(self):
        """
        Returns an array where we get the GPS-derived values for each element of the treatment grid.
        Resulting array will be of shape (n_samples, treatment_grid_num)
        """
        # Creates an empty 2d array of shape (n_samples, treatment_grid_num)
        gps_at_grid = np.zeros((len(self.T), self.treatment_grid_num), dtype=float)

        # Loop over all grid values
        for i in range(0, self.treatment_grid_num):
        	gps_at_grid[:,i] = self.gps_function(self.grid_values[i])

        return gps_at_grid


    def print_gam_summary(self):
        """
        Very simple, just prints the GAM model summary (uses pyGAM's output)
        """
        print(self._gam_summary_str)


    def _fit_gam(self):
        """
        Fits a GAM that predicts the outcome from the treatment and GPS
        """

        X = np.column_stack((self.T.values, self.gps))
        y = np.asarray(self.y)

        return LinearGAM(s(0, n_splines=self.n_splines, spline_order=self.spline_order) + s(1, n_splines=self.n_splines, spline_order=self.spline_order), max_iter=500).fit(X, y)


    def _create_normal_gps_function(self):
        """
        Models the GPS using a GLM of the Gaussian family
        """
        normal_gps_model = sm.GLM(self.T, self.X, family=sm.families.Gaussian()).fit()


        pred_treat = normal_gps_model.fittedvalues
        sigma = np.std(normal_gps_model.resid_response)

        def gps_function(treatment_val, pred_treat = pred_treat, sigma = sigma):
            return norm.pdf(treatment_val, pred_treat, sigma)

        return gps_function, normal_gps_model.deviance


    def _create_lognormal_gps_function(self):
        """
        Models the GPS using a GLM of the Gaussian family (assumes treatment is lognormal)
        """
        lognormal_gps_model = sm.GLM(np.log(self.T), self.X, family=sm.families.Gaussian()).fit()

        pred_log_treat = lognormal_gps_model.fittedvalues
        sigma = np.std(lognormal_gps_model.resid_response)

        def gps_function(treatment_val, pred_log_treat = pred_log_treat, sigma = sigma):
            return norm.pdf(np.log(treatment_val), pred_log_treat, sigma)

        return gps_function, lognormal_gps_model.deviance


    def _create_gamma_gps_function(self):
        """
        Models the GPS using a GLM of the Gamma family
        """
        gamma_gps_model = sm.GLM(self.T, self.X, family=sm.families.Gamma(Inverse_Power())).fit()

        mu = gamma_gps_model.mu
        scale = gamma_gps_model.scale
        shape = (mu / gamma_gps_model.scale)

        def gps_function(treatment_val):
            return gamma.pdf(treatment_val, a = shape, loc = 0, scale = scale)

        return gps_function, gamma_gps_model.deviance


    def _find_best_gps_model(self):
        """
        If user doesn't provide a GLM family for modeling the GPS, this function compares
        a few different gps models and picks the one with the lowest deviance
        """
        models_to_try_dict = {
            'normal_gps_model': self._create_normal_gps_function(),
            'lognormal_gps_model': self._create_lognormal_gps_function(),
            'gamma_gps_model': self._create_gamma_gps_function(),

        }

        model_comparison_dict = {}

        for key, value in models_to_try_dict.items():
            model_comparison_dict[key] = value[1]

        # Return model with lowest deviance
        best_model = min(model_comparison_dict, key=model_comparison_dict.get)

        return best_model, models_to_try_dict[best_model][0], models_to_try_dict[best_model][1]