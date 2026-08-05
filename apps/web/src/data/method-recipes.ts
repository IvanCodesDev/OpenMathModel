export type MethodRecipe = {
  /** KaTeX 可渲染的 LaTeX 源码，不含定界符。 */
  formula: string;
  code: {
    python: string;
    r: string;
    /** MATLAB 语法，绝大多数片段在 Octave 下同样可运行；不兼容处已在注释中标注。 */
    matlab: string;
  };
};

export type RecipeLanguage = keyof MethodRecipe["code"];

export const RECIPE_LANGUAGES: { id: RecipeLanguage; label: string }[] = [
  { id: "python", label: "Python" },
  { id: "r", label: "R" },
  { id: "matlab", label: "MATLAB / Octave" },
];

/** 按方法 id 关联 method-library.ts 中的条目。 */
export const methodRecipes: Record<string, MethodRecipe> = {
  "data-cleaning": {
    formula: "\\text{IQR 判据：} x < Q_1 - 1.5\\,\\mathrm{IQR} \\;\\lor\\; x > Q_3 + 1.5\\,\\mathrm{IQR},\\quad \\mathrm{IQR}=Q_3-Q_1",
    code: {
      python: "import pandas as pd\nfrom sklearn.experimental import enable_iterative_imputer  # noqa: F401\nfrom sklearn.impute import IterativeImputer\n\nq1, q3 = df[col].quantile([.25, .75])\niqr = q3 - q1\ndf['is_outlier'] = (df[col] < q1 - 1.5 * iqr) | (df[col] > q3 + 1.5 * iqr)\nimputer = IterativeImputer(random_state=42)\ndf[num_cols] = imputer.fit_transform(df[num_cols])",
      r: "library(mice)\n\nq <- quantile(df[[col]], c(.25, .75), na.rm = TRUE)\niqr <- q[2] - q[1]\ndf$is_outlier <- df[[col]] < q[1] - 1.5 * iqr | df[[col]] > q[2] + 1.5 * iqr\n\nimp <- mice(df[num_cols], m = 5, method = \"pmm\", seed = 42, printFlag = FALSE)\ndf[num_cols] <- complete(imp, 1)",
      matlab: "q = quantile(T.(col), [0.25 0.75]);\niqr_v = q(2) - q(1);\nT.is_outlier = T.(col) < q(1) - 1.5*iqr_v | T.(col) > q(2) + 1.5*iqr_v;\n\n% Octave 无 fillmissing，改用 inpaint/手工中位数填补\nX = T{:, num_cols};\nfor j = 1:size(X, 2)\n    m = isnan(X(:, j));\n    X(m, j) = median(X(~m, j));\nend\nT{:, num_cols} = X;",
    },
  },
  "interpolation-fitting": {
    formula: "\\min_{\\theta}\\sum_{i=1}^{n}\\bigl[y_i - f(x_i;\\theta)\\bigr]^2 \\qquad \\text{三次样条在节点处 } S''(x_k^-) = S''(x_k^+)",
    code: {
      python: "import numpy as np\nfrom scipy.interpolate import CubicSpline\nfrom scipy.optimize import curve_fit\n\nspline = CubicSpline(x_obs, y_obs, bc_type='natural')\ny_dense = spline(x_dense)\n\ndef model(x, a, b, c):\n    return a * np.exp(-b * x) + c\n\nparams, cov = curve_fit(model, x_obs, y_obs, p0=[1, .1, 0])\nstd_err = np.sqrt(np.diag(cov))",
      r: "spl <- splinefun(x_obs, y_obs, method = \"natural\")\ny_dense <- spl(x_dense)\n\nfit <- nls(y ~ a * exp(-b * x) + c,\n           data = data.frame(x = x_obs, y = y_obs),\n           start = list(a = 1, b = .1, c = 0))\nsummary(fit)\nconfint(fit)",
      matlab: "y_dense = spline(x_obs, y_obs, x_dense);   % 三次样条插值\n\nmodel = @(p, x) p(1) * exp(-p(2) * x) + p(3);\np0 = [1, 0.1, 0];\n[p, resnorm, residual, ~, ~, ~, J] = lsqcurvefit(model, p0, x_obs, y_obs);\nci = nlparci(p, residual, 'jacobian', J);",
    },
  },
  "correlation-diagnosis": {
    formula: "\\mathrm{VIF}_j = \\frac{1}{1 - R_j^2},\\qquad R_j^2 \\text{ 为第 } j \\text{ 个自变量对其余自变量回归的判定系数}",
    code: {
      python: "import pandas as pd\nfrom statsmodels.stats.outliers_influence import variance_inflation_factor\n\ncorr = df[features].corr(method='spearman')\nvif = pd.Series(\n    [variance_inflation_factor(df[features].values, i) for i in range(len(features))],\n    index=features,\n).sort_values(ascending=False)",
      r: "library(car)\n\ncorr <- cor(df[features], method = \"spearman\", use = \"pairwise.complete.obs\")\ncorrplot::corrplot(corr, order = \"hclust\")\n\nfit <- lm(y ~ ., data = df[c(features, \"y\")])\nsort(vif(fit), decreasing = TRUE)",
      matlab: "R = corr(X, 'Type', 'Spearman', 'Rows', 'pairwise');\nheatmap(features, features, R);\n\n% 逐列回归求 VIF\nvif = zeros(1, size(X, 2));\nfor j = 1:size(X, 2)\n    others = X(:, [1:j-1, j+1:end]);\n    mdl = fitlm(others, X(:, j));\n    vif(j) = 1 / (1 - mdl.Rsquared.Ordinary);\nend",
    },
  },
  "linear-regression": {
    formula: "\\hat{y} = X\\hat{\\beta},\\qquad \\hat{\\beta} = (X^{\\mathsf{T}}X)^{-1}X^{\\mathsf{T}}y",
    code: {
      python: "import statsmodels.api as sm\nX_design = sm.add_constant(X)\nfit = sm.OLS(y, X_design).fit(cov_type='HC3')\nprint(fit.summary())\ny_pred = fit.predict(sm.add_constant(X_test))",
      r: "library(sandwich); library(lmtest)\n\nfit <- lm(y ~ ., data = train)\ncoeftest(fit, vcov. = vcovHC(fit, type = \"HC3\"))   # 稳健标准误\npar(mfrow = c(2, 2)); plot(fit)                    # 残差诊断四图\ny_pred <- predict(fit, newdata = test)",
      matlab: "mdl = fitlm(X, y);\ndisp(mdl)\n\n% 稳健协方差下的系数检验\n[p, F] = coefTest(mdl);\nplotResiduals(mdl, 'fitted');\ny_pred = predict(mdl, X_test);\nci = coefCI(mdl, 0.05);",
    },
  },
  "regularized-regression": {
    formula: "\\text{Ridge: } \\min_{\\beta}\\;\\lVert y - X\\beta\\rVert_2^2 + \\lambda\\lVert\\beta\\rVert_2^2 \\qquad \\text{LASSO: } \\min_{\\beta}\\;\\lVert y - X\\beta\\rVert_2^2 + \\lambda\\lVert\\beta\\rVert_1",
    code: {
      python: "from sklearn.linear_model import LassoCV, RidgeCV\nfrom sklearn.pipeline import make_pipeline\nfrom sklearn.preprocessing import StandardScaler\n\nlasso = make_pipeline(StandardScaler(), LassoCV(cv=10, random_state=42))\nlasso.fit(X_train, y_train)\nselected = X.columns[lasso[-1].coef_ != 0]",
      r: "library(glmnet)\n\nX <- scale(as.matrix(train[features]))\ncv <- cv.glmnet(X, train$y, alpha = 1, nfolds = 10)   # alpha=1 为 LASSO\nplot(cv)\nselected <- rownames(coef(cv, s = \"lambda.1se\"))[which(coef(cv, s = \"lambda.1se\") != 0)]",
      matlab: "Xz = zscore(X);\n[B, FitInfo] = lasso(Xz, y, 'CV', 10, 'Alpha', 1);\nidx = FitInfo.Index1SE;\nselected = find(B(:, idx) ~= 0);\nlassoPlot(B, FitInfo, 'PlotType', 'CV');",
    },
  },
  "principal-component-analysis": {
    formula: "Z = XW,\\qquad \\frac{1}{n-1}X^{\\mathsf{T}}X\\,w_k = \\lambda_k w_k,\\quad \\lambda_1 \\ge \\lambda_2 \\ge \\cdots \\ge \\lambda_p",
    code: {
      python: "from sklearn.preprocessing import StandardScaler\nfrom sklearn.decomposition import PCA\nXz = StandardScaler().fit_transform(X)\npca = PCA(n_components=.90, svd_solver='full')\nscore = pca.fit_transform(Xz)\nprint(pca.explained_variance_ratio_.cumsum())",
      r: "pca <- prcomp(X, center = TRUE, scale. = TRUE)\nsummary(pca)                       # 累计解释方差\nscore <- pca$x[, 1:k]\nbiplot(pca, scale = 0)\ncumsum(pca$sdev^2 / sum(pca$sdev^2))",
      matlab: "Xz = zscore(X);\n[coeff, score, latent, ~, explained] = pca(Xz);\ncumsum(explained)                 % 累计解释方差百分比\nbiplot(coeff(:, 1:2), 'Scores', score(:, 1:2));",
    },
  },
  "hypothesis-testing": {
    formula: "p = \\Pr\\bigl(T \\ge T_{\\text{obs}} \\mid H_0\\bigr),\\qquad \\text{当 } p < \\alpha \\text{ 时拒绝 } H_0",
    code: {
      python: "from scipy import stats\nt, p = stats.ttest_ind(group_a, group_b, equal_var=False)\neffect = (group_a.mean() - group_b.mean()) / pooled_sd\nci = stats.bootstrap((group_a, group_b), statistic=mean_difference).confidence_interval",
      r: "res <- t.test(group_a, group_b, var.equal = FALSE)   # Welch 检验\nres$p.value; res$conf.int\n\nlibrary(effectsize)\ncohens_d(group_a, group_b)                          # 效应量与区间\np.adjust(p_values, method = \"BH\")                   # 多重比较校正",
      matlab: "[h, p, ci, stats] = ttest2(group_a, group_b, 'Vartype', 'unequal');\n\n% Cohen's d 效应量\nsp = sqrt(((numel(group_a)-1)*var(group_a) + (numel(group_b)-1)*var(group_b)) / ...\n          (numel(group_a) + numel(group_b) - 2));\nd = (mean(group_a) - mean(group_b)) / sp;\np_adj = mafdr(p_values, 'BHFDR', true);",
    },
  },
  "markov-chain": {
    formula: "\\Pr(X_{n+1}=j \\mid X_n=i)=p_{ij},\\qquad \\pi P = \\pi,\\quad \\textstyle\\sum_i \\pi_i = 1",
    code: {
      python: "import numpy as np\n\ncounts = np.zeros((n_states, n_states))\nfor a, b in zip(seq[:-1], seq[1:]):\n    counts[a, b] += 1\nP = counts / counts.sum(1, keepdims=True)\n\nvals, vecs = np.linalg.eig(P.T)\npi = np.real(vecs[:, np.argmin(abs(vals - 1))])\npi = pi / pi.sum()",
      r: "library(markovchain)\n\nmc <- markovchainFit(data = seq)$estimate\nsteadyStates(mc)                    # 平稳分布\nmeanFirstPassageTime(mc)            # 平均首达时间\nmc ^ 5                              # 5 步转移矩阵",
      matlab: "counts = accumarray([seq(1:end-1)', seq(2:end)'], 1, [n n]);\nP = counts ./ sum(counts, 2);\n\n[V, D] = eig(P');\n[~, k] = min(abs(diag(D) - 1));\npi_s = real(V(:, k));\npi_s = pi_s / sum(pi_s);           % 平稳分布",
    },
  },
  "gm11": {
    formula: "\\frac{\\mathrm{d}x^{(1)}}{\\mathrm{d}t} + a\\,x^{(1)} = b,\\qquad \\hat{x}^{(1)}(k+1)=\\Bigl(x^{(0)}(1)-\\frac{b}{a}\\Bigr)e^{-ak}+\\frac{b}{a}",
    code: {
      python: "import numpy as np\n\nx0 = np.asarray(series, dtype=float)\nx1 = x0.cumsum()\nz1 = (x1[:-1] + x1[1:]) / 2\nB = np.c_[-z1, np.ones(len(z1))]\na, b = np.linalg.lstsq(B, x0[1:], rcond=None)[0]\nk = np.arange(len(x0) + horizon)\nx1_hat = (x0[0] - b / a) * np.exp(-a * k) + b / a\nx0_hat = np.r_[x1_hat[0], np.diff(x1_hat)]",
      r: "x0 <- as.numeric(series)\nx1 <- cumsum(x0)\nz1 <- (head(x1, -1) + tail(x1, -1)) / 2\nB  <- cbind(-z1, 1)\nab <- solve(t(B) %*% B) %*% t(B) %*% x0[-1]\na  <- ab[1]; b <- ab[2]\n\nk      <- 0:(length(x0) + horizon - 1)\nx1_hat <- (x0[1] - b / a) * exp(-a * k) + b / a\nx0_hat <- c(x1_hat[1], diff(x1_hat))",
      matlab: "x0 = series(:);\nx1 = cumsum(x0);\nz1 = (x1(1:end-1) + x1(2:end)) / 2;\nB  = [-z1, ones(numel(z1), 1)];\nab = B \\ x0(2:end);\na = ab(1); b = ab(2);\n\nk = (0:numel(x0) + horizon - 1)';\nx1_hat = (x0(1) - b/a) * exp(-a*k) + b/a;\nx0_hat = [x1_hat(1); diff(x1_hat)];",
    },
  },
  "arima": {
    formula: "\\varphi(B)\\,(1-B)^d y_t = c + \\theta(B)\\,\\varepsilon_t,\\qquad \\varepsilon_t \\sim \\mathrm{WN}(0,\\sigma^2)",
    code: {
      python: "from statsmodels.tsa.arima.model import ARIMA\nfit = ARIMA(train, order=(p, d, q)).fit()\nforecast = fit.get_forecast(steps=h)\ny_pred = forecast.predicted_mean\ninterval = forecast.conf_int(alpha=.05)",
      r: "library(forecast)\n\nfit <- auto.arima(train, seasonal = FALSE, stepwise = FALSE)\nsummary(fit)\ncheckresiduals(fit)                 # Ljung-Box 白噪声检验\nfc <- forecast(fit, h = h, level = 95)\naccuracy(fc, test)",
      matlab: "mdl = arima('ARLags', 1:p, 'D', d, 'MALags', 1:q);\nest = estimate(mdl, train);\n\n[yF, yMSE] = forecast(est, h, 'Y0', train);\nlower = yF - 1.96 * sqrt(yMSE);\nupper = yF + 1.96 * sqrt(yMSE);\n[hL, pL] = lbqtest(infer(est, train));   % 残差白噪声检验",
    },
  },
  "xgboost": {
    formula: "\\mathcal{L}^{(t)} = \\sum_{i=1}^{n} l\\bigl(y_i,\\;\\hat{y}_i^{(t-1)}+f_t(x_i)\\bigr) + \\Omega(f_t),\\qquad \\Omega(f)=\\gamma T+\\tfrac{1}{2}\\lambda\\lVert w\\rVert^2",
    code: {
      python: "from xgboost import XGBRegressor\nmodel = XGBRegressor(n_estimators=500, max_depth=6, learning_rate=.05,\n                     subsample=.8, colsample_bytree=.8, random_state=42)\nmodel.fit(X_train, y_train, eval_set=[(X_valid, y_valid)], verbose=False)\ny_pred = model.predict(X_test)",
      r: "library(xgboost)\n\ndtrain <- xgb.DMatrix(as.matrix(X_train), label = y_train)\ndvalid <- xgb.DMatrix(as.matrix(X_valid), label = y_valid)\nparams <- list(objective = \"reg:squarederror\", max_depth = 6,\n               eta = .05, subsample = .8, colsample_bytree = .8)\nfit <- xgb.train(params, dtrain, nrounds = 500,\n                 watchlist = list(valid = dvalid), early_stopping_rounds = 30)\nxgb.importance(model = fit)",
      matlab: "% MATLAB 无官方 XGBoost，用等价的提升树集成\nt = templateTree('MaxNumSplits', 63, 'MinLeafSize', 3);\nmdl = fitrensemble(X_train, y_train, 'Method', 'LSBoost', ...\n    'NumLearningCycles', 500, 'LearnRate', 0.05, 'Learners', t);\n\ny_pred = predict(mdl, X_test);\nimp = predictorImportance(mdl);",
    },
  },
  "bp-neural-network": {
    formula: "\\delta^{(l)} = \\bigl(W^{(l+1)\\mathsf{T}}\\delta^{(l+1)}\\bigr)\\odot\\sigma'\\bigl(z^{(l)}\\bigr),\\qquad W^{(l)} \\leftarrow W^{(l)} - \\eta\\,\\delta^{(l)}a^{(l-1)\\mathsf{T}}",
    code: {
      python: "from sklearn.neural_network import MLPRegressor\nfrom sklearn.pipeline import make_pipeline\nfrom sklearn.preprocessing import StandardScaler\n\nmodel = make_pipeline(\n    StandardScaler(),\n    MLPRegressor(hidden_layer_sizes=(64, 32), early_stopping=True,\n                 max_iter=2000, random_state=42),\n)\nmodel.fit(X_train, y_train)",
      r: "library(neuralnet)\n\nscaled <- as.data.frame(scale(train))\nnet <- neuralnet(y ~ ., data = scaled, hidden = c(64, 32),\n                 linear.output = TRUE, stepmax = 1e6, rep = 5)\nplot(net)\npred <- compute(net, scale(test[features]))$net.result",
      matlab: "net = fitnet([64 32]);\nnet.divideParam.trainRatio = 0.7;\nnet.divideParam.valRatio   = 0.15;\nnet.divideParam.testRatio  = 0.15;\nnet.trainParam.max_fail = 20;          % 早停\n\n[net, tr] = train(net, X', y');\ny_pred = net(X_test');\nplotperform(tr);",
    },
  },
  "lstm": {
    formula: "c_t = f_t \\odot c_{t-1} + i_t \\odot \\tilde{c}_t,\\qquad h_t = o_t \\odot \\tanh(c_t)",
    code: {
      python: "import torch\nmodel = torch.nn.LSTM(input_size=n_features, hidden_size=64, batch_first=True)\nsequence, (h, c) = model(X_batch)\ny_pred = head(sequence[:, -1])\nloss = torch.nn.functional.mse_loss(y_pred, y_batch)",
      r: "library(keras)\n\nmodel <- keras_model_sequential() %>%\n  layer_lstm(units = 64, input_shape = c(timesteps, n_features)) %>%\n  layer_dropout(.2) %>%\n  layer_dense(units = 1)\n\nmodel %>% compile(optimizer = \"adam\", loss = \"mse\")\nmodel %>% fit(X_train, y_train, epochs = 100, batch_size = 32,\n              validation_split = .2,\n              callbacks = list(callback_early_stopping(patience = 10)))",
      matlab: "layers = [\n    sequenceInputLayer(numFeatures)\n    lstmLayer(64, 'OutputMode', 'last')\n    dropoutLayer(0.2)\n    fullyConnectedLayer(1)\n    regressionLayer];\n\nopts = trainingOptions('adam', 'MaxEpochs', 100, ...\n    'ValidationData', {XVal, YVal}, 'ValidationPatience', 10, ...\n    'Shuffle', 'never');            % 时序数据不可打乱\nnet = trainNetwork(XTrain, YTrain, layers, opts);",
    },
  },
  "prophet": {
    formula: "y(t) = g(t) + s(t) + h(t) + \\varepsilon_t,\\qquad s(t)=\\sum_{n=1}^{N}\\Bigl(a_n\\cos\\tfrac{2\\pi nt}{P}+b_n\\sin\\tfrac{2\\pi nt}{P}\\Bigr)",
    code: {
      python: "from prophet import Prophet\nmodel = Prophet(yearly_seasonality=True, changepoint_prior_scale=.05)\nmodel.add_country_holidays(country_name='CN')\nmodel.fit(train[['ds', 'y']])\nforecast = model.predict(model.make_future_dataframe(periods=h))",
      r: "library(prophet)\n\nm <- prophet(train, yearly.seasonality = TRUE, changepoint.prior.scale = .05)\nfuture <- make_future_dataframe(m, periods = h)\nfc <- predict(m, future)\nprophet_plot_components(m, fc)\n\ncv <- cross_validation(m, horizon = h, units = \"days\")\nperformance_metrics(cv)",
      matlab: "% MATLAB 无 Prophet，用等价的趋势 + 傅里叶季节项回归\nt = (1:numel(y))';\nP = 365.25; N = 10;\nF = [];\nfor n = 1:N\n    F = [F, cos(2*pi*n*t/P), sin(2*pi*n*t/P)];\nend\nX = [ones(size(t)), t, F];         % 常数 + 线性趋势 + 季节\nbeta = X \\ y;\ny_fit = X * beta;",
    },
  },
  "logistic-regression": {
    formula: "\\Pr(y=1 \\mid x) = \\frac{1}{1+e^{-(\\beta_0+\\beta^{\\mathsf{T}}x)}},\\qquad \\log\\frac{p}{1-p} = \\beta_0 + \\beta^{\\mathsf{T}}x",
    code: {
      python: "from sklearn.linear_model import LogisticRegression\nmodel = LogisticRegression(C=1.0, class_weight='balanced', max_iter=2000)\nmodel.fit(X_train, y_train)\nprob = model.predict_proba(X_test)[:, 1]",
      r: "fit <- glm(y ~ ., data = train, family = binomial())\nsummary(fit)\nexp(cbind(OR = coef(fit), confint(fit)))      # 优势比及区间\n\nprob <- predict(fit, newdata = test, type = \"response\")\nlibrary(pROC); roc(test$y, prob)",
      matlab: "[B, dev, stats] = mnrfit(X_train, categorical(y_train));\nprob = mnrval(B, X_test);\n\nOR = exp(B);                                  % 优势比\n[Xr, Yr, T, AUC] = perfcurve(y_test, prob(:, 2), 1);\nplot(Xr, Yr); title(sprintf('AUC = %.3f', AUC));",
    },
  },
  "svm": {
    formula: "\\min_{w,b,\\xi}\\;\\tfrac{1}{2}\\lVert w\\rVert^2 + C\\sum_{i=1}^{n}\\xi_i \\quad \\text{s.t.}\\quad y_i\\bigl(w^{\\mathsf{T}}\\phi(x_i)+b\\bigr) \\ge 1-\\xi_i,\\;\\xi_i \\ge 0",
    code: {
      python: "from sklearn.pipeline import make_pipeline\nfrom sklearn.preprocessing import StandardScaler\nfrom sklearn.svm import SVC\nmodel = make_pipeline(StandardScaler(), SVC(C=10, gamma='scale', probability=True))\nmodel.fit(X_train, y_train)",
      r: "library(e1071)\n\ntuned <- tune.svm(y ~ ., data = train, kernel = \"radial\",\n                  cost = 10^(-1:3), gamma = 10^(-3:1),\n                  tunecontrol = tune.control(cross = 10))\nfit <- tuned$best.model\nsummary(fit)                                  # 含支持向量数\npred <- predict(fit, test, probability = TRUE)",
      matlab: "mdl = fitcsvm(X_train, y_train, 'KernelFunction', 'rbf', ...\n    'Standardize', true, 'OptimizeHyperparameters', {'BoxConstraint', 'KernelScale'});\n\nmdl = fitPosterior(mdl);                      % 概率校准\n[label, score] = predict(mdl, X_test);\nsv_ratio = size(mdl.SupportVectors, 1) / size(X_train, 1);",
    },
  },
  "random-forest": {
    formula: "\\hat{y} = \\operatorname*{mode}_{b=1..B}\\{T_b(x)\\}\\ \\text{(分类)},\\qquad \\hat{y} = \\frac{1}{B}\\sum_{b=1}^{B}T_b(x)\\ \\text{(回归)}",
    code: {
      python: "from sklearn.ensemble import RandomForestClassifier\nmodel = RandomForestClassifier(n_estimators=600, min_samples_leaf=3,\n                               class_weight='balanced', n_jobs=-1, random_state=42)\nmodel.fit(X_train, y_train)",
      r: "library(randomForest)\n\nfit <- randomForest(y ~ ., data = train, ntree = 600,\n                    nodesize = 3, importance = TRUE)\nprint(fit)                                # 含 OOB 误差\nvarImpPlot(fit, type = 1)                 # 置换重要性\npred <- predict(fit, test, type = \"prob\")",
      matlab: "mdl = TreeBagger(600, X_train, y_train, 'Method', 'classification', ...\n    'MinLeafSize', 3, 'OOBPrediction', 'on', ...\n    'OOBPredictorImportance', 'on');\n\nplot(oobError(mdl)); xlabel('树数'); ylabel('OOB 误差');\nimp = mdl.OOBPermutedPredictorDeltaError;\n[label, score] = predict(mdl, X_test);",
    },
  },
  "kmeans": {
    formula: "\\min_{C_1,\\dots,C_K}\\;\\sum_{k=1}^{K}\\sum_{x_i \\in C_k}\\lVert x_i-\\mu_k\\rVert_2^2,\\qquad \\mu_k=\\frac{1}{|C_k|}\\sum_{x_i \\in C_k}x_i",
    code: {
      python: "from sklearn.cluster import KMeans\nmodel = KMeans(n_clusters=k, init='k-means++', n_init=30, random_state=42)\nlabels = model.fit_predict(X_scaled)\ncenters = model.cluster_centers_",
      r: "set.seed(42)\nXz <- scale(X)\nfit <- kmeans(Xz, centers = k, nstart = 30, algorithm = \"Hartigan-Wong\")\n\nlibrary(cluster)\nsil <- silhouette(fit$cluster, dist(Xz))\nmean(sil[, 3])\nfactoextra::fviz_nbclust(Xz, kmeans, method = \"silhouette\")",
      matlab: "Xz = zscore(X);\nrng(42);\n[labels, C, sumd] = kmeans(Xz, k, 'Replicates', 30, 'Start', 'plus');\n\ns = silhouette(Xz, labels);\nfprintf('平均轮廓系数 %.3f\\n', mean(s));\nevalclusters(Xz, 'kmeans', 'silhouette', 'KList', 2:10);",
    },
  },
  "hierarchical-clustering": {
    formula: "d(A,B)=\\begin{cases}\\min_{a\\in A,b\\in B} d(a,b) & \\text{single}\\\\ \\max_{a\\in A,b\\in B} d(a,b) & \\text{complete}\\\\ \\frac{|A||B|}{|A|+|B|}\\lVert\\bar{a}-\\bar{b}\\rVert^2 & \\text{Ward}\\end{cases}",
    code: {
      python: "from scipy.cluster.hierarchy import linkage, fcluster, cophenet\nfrom scipy.spatial.distance import pdist\n\nD = pdist(X_scaled)\nZ = linkage(D, method='ward')\ncoph, _ = cophenet(Z, D)\nlabels = fcluster(Z, t=k, criterion='maxclust')",
      r: "d  <- dist(scale(X), method = \"euclidean\")\nhc <- hclust(d, method = \"ward.D2\")\nplot(hc, hang = -1)\n\ncoph <- cor(d, cophenetic(hc))            # 树状图保真度\nlabels <- cutree(hc, k = k)\nrect.hclust(hc, k = k, border = \"red\")",
      matlab: "Xz = zscore(X);\nD  = pdist(Xz, 'euclidean');\nZ  = linkage(D, 'ward');\n\ndendrogram(Z, 0);\ncoph = cophenet(Z, D);                    % 保真度\nlabels = cluster(Z, 'maxclust', k);",
    },
  },
  "dbscan": {
    formula: "N_\\varepsilon(p)=\\{q \\mid d(p,q)\\le\\varepsilon\\},\\qquad p \\text{ 为核心点} \\iff \\lvert N_\\varepsilon(p)\\rvert \\ge \\mathrm{MinPts}",
    code: {
      python: "from sklearn.cluster import DBSCAN\nmodel = DBSCAN(eps=.35, min_samples=8, metric='euclidean')\nlabels = model.fit_predict(X_scaled)\nnoise_rate = (labels == -1).mean()",
      r: "library(dbscan)\n\nkNNdistplot(Xz, k = 8); abline(h = .35, lty = 2)   # 用拐点选 eps\nfit <- dbscan(Xz, eps = .35, minPts = 8)\ntable(fit$cluster)\nnoise_rate <- mean(fit$cluster == 0)",
      matlab: "% 先用 k-distance 曲线定 eps\nkD = pdist2(Xz, Xz, 'euclidean', 'Smallest', 9);\nplot(sort(kD(end, :)));\n\nlabels = dbscan(Xz, 0.35, 8);\nnoise_rate = mean(labels == -1);",
    },
  },
  "linear-programming": {
    formula: "\\min_{x}\\;c^{\\mathsf{T}}x \\quad \\text{s.t.}\\quad Ax \\le b,\\;\\; A_{\\mathrm{eq}}x = b_{\\mathrm{eq}},\\;\\; l \\le x \\le u",
    code: {
      python: "from scipy.optimize import linprog\nresult = linprog(c, A_ub=A, b_ub=b, bounds=bounds, method='highs')\nassert result.success\nx_star, objective = result.x, result.fun\nshadow_price = result.ineqlin.marginals",
      r: "library(lpSolve)\n\nres <- lp(direction = \"min\", objective.in = c,\n          const.mat = A, const.dir = rep(\"<=\", nrow(A)), const.rhs = b,\n          compute.sens = 1)\nres$objval\nres$solution\nres$duals                                  # 影子价格",
      matlab: "options = optimoptions('linprog', 'Display', 'off');\n[x, fval, exitflag, ~, lambda] = linprog(c, A, b, Aeq, beq, lb, ub, options);\n\nassert(exitflag == 1, '未找到最优解');\nshadow_price = lambda.ineqlin;             % 影子价格",
    },
  },
  "mixed-integer-programming": {
    formula: "\\min_{x}\\;c^{\\mathsf{T}}x \\quad \\text{s.t.}\\quad Ax \\le b,\\;\\; x_j \\in \\mathbb{Z}\\;(j \\in I),\\qquad \\mathrm{Gap}=\\frac{z_{\\mathrm{UB}}-z_{\\mathrm{LB}}}{\\lvert z_{\\mathrm{UB}}\\rvert}",
    code: {
      python: "import pulp\nmodel = pulp.LpProblem('plan', pulp.LpMinimize)\nx = pulp.LpVariable.dicts('x', items, lowBound=0, cat='Integer')\nmodel += pulp.lpSum(cost[i] * x[i] for i in items)\nmodel.solve(pulp.PULP_CBC_CMD(msg=False))\ngap = abs(pulp.value(model.objective) - model.bestBound)",
      r: "library(ompr); library(ompr.roi); library(ROI.plugin.glpk)\n\nmodel <- MIPModel() %>%\n  add_variable(x[i], i = 1:n, type = \"integer\", lb = 0) %>%\n  set_objective(sum_expr(cost[i] * x[i], i = 1:n), \"min\") %>%\n  add_constraint(sum_expr(a[i] * x[i], i = 1:n) <= b)\n\nres <- solve_model(model, with_ROI(solver = \"glpk\", verbose = TRUE))\nget_solution(res, x[i])",
      matlab: "options = optimoptions('intlinprog', 'Display', 'iter', 'RelativeGapTolerance', 1e-4);\n[x, fval, exitflag, output] = intlinprog(c, intcon, A, b, Aeq, beq, lb, ub, options);\n\nfprintf('目标值 %.4f，相对间隙 %.4f%%\\n', fval, output.relativegap * 100);",
    },
  },
  "dynamic-programming": {
    formula: "f(k,s) = \\operatorname*{opt}_{u \\in U(k,s)}\\Bigl\\{r(k,s,u) + f\\bigl(k+1,\\;T(k,s,u)\\bigr)\\Bigr\\},\\qquad f(N,s)=g(s)",
    code: {
      python: "from functools import lru_cache\n\n@lru_cache(maxsize=None)\ndef f(stage: int, state: int) -> float:\n    if stage == n_stages:\n        return terminal_value(state)\n    return max(\n        reward(stage, state, u) + f(stage + 1, transit(stage, state, u))\n        for u in feasible_actions(stage, state)\n    )\n\nbest = f(0, initial_state)",
      r: "memo <- new.env(hash = TRUE)\n\nf <- function(stage, state) {\n  key <- paste(stage, state, sep = \"-\")\n  if (!is.null(memo[[key]])) return(memo[[key]])\n  if (stage == n_stages) return(terminal_value(state))\n  best <- max(vapply(feasible_actions(stage, state), function(u)\n    reward(stage, state, u) + f(stage + 1, transit(stage, state, u)), numeric(1)))\n  memo[[key]] <- best\n  best\n}\nf(0, initial_state)",
      matlab: "V = -inf(n_stages + 1, n_states);\npolicy = zeros(n_stages, n_states);\nV(end, :) = terminal_value(1:n_states);\n\nfor k = n_stages:-1:1                      % 自底向上递推\n    for s = 1:n_states\n        for u = feasible_actions(k, s)\n            v = reward(k, s, u) + V(k + 1, transit(k, s, u));\n            if v > V(k, s)\n                V(k, s) = v; policy(k, s) = u;\n            end\n        end\n    end\nend",
    },
  },
  "multi-objective": {
    formula: "\\min_{x \\in \\Omega}\\;F(x)=\\bigl[f_1(x),\\dots,f_k(x)\\bigr];\\quad x^* \\text{ 为帕累托最优} \\iff \\nexists\\,x:\\;\\forall i\\,f_i(x)\\le f_i(x^*)\\;\\land\\;\\exists j\\,f_j(x)<f_j(x^*)",
    code: {
      python: "from pymoo.algorithms.moo.nsga2 import NSGA2\nfrom pymoo.optimize import minimize\nresult = minimize(problem, NSGA2(pop_size=120), ('n_gen', 300), seed=42)\npareto_x, pareto_f = result.X, result.F",
      r: "library(mco)\n\nres <- nsga2(fn = function(x) c(f1(x), f2(x)),\n             idim = n_var, odim = 2,\n             lower.bounds = lb, upper.bounds = ub,\n             popsize = 120, generations = 300)\n\npareto <- res$value[res$pareto.optimal, ]\nplot(pareto, xlab = \"f1\", ylab = \"f2\")",
      matlab: "options = optimoptions('gamultiobj', 'PopulationSize', 120, ...\n    'MaxGenerations', 300, 'ParetoFraction', 0.35, 'PlotFcn', @gaplotpareto);\n\n[x_pareto, f_pareto] = gamultiobj(@objectives, nvars, A, b, Aeq, beq, lb, ub, options);\nhv = hypervolume(f_pareto, ref_point);     % 需自备 hypervolume 实现",
    },
  },
  "genetic-algorithm": {
    formula: "\\Pr(x_i \\text{ 被选中}) = \\frac{\\mathrm{fit}(x_i)}{\\sum_{j=1}^{N}\\mathrm{fit}(x_j)},\\qquad \\text{适应度} = f(x) + \\rho\\sum_k \\max\\bigl(0,\\,g_k(x)\\bigr)",
    code: {
      python: "from deap import algorithms\n# toolbox 中注册 individual、evaluate、mate、mutate、select\npopulation = toolbox.population(n=200)\nfinal, log = algorithms.eaMuPlusLambda(population, toolbox, 200, 400,\n                                       cxpb=.7, mutpb=.2, ngen=300)",
      r: "library(GA)\n\nres <- ga(type = \"real-valued\", fitness = function(x) -objective(x),\n          lower = lb, upper = ub,\n          popSize = 200, maxiter = 300, pcrossover = .7, pmutation = .2,\n          elitism = 10, seed = 42)\nsummary(res)\nplot(res)",
      matlab: "options = optimoptions('ga', 'PopulationSize', 200, 'MaxGenerations', 300, ...\n    'CrossoverFraction', 0.7, 'EliteCount', 10, 'PlotFcn', @gaplotbestf);\n\nbest = inf(10, 1);\nfor s = 1:10                                   % 多种子重复\n    rng(s);\n    [x, fval] = ga(@objective, nvars, A, b, Aeq, beq, lb, ub, @nonlcon, options);\n    best(s) = fval;\nend\nfprintf('均值 %.4f 标准差 %.4f\\n', mean(best), std(best));",
    },
  },
  "simulated-annealing": {
    formula: "\\Pr(\\text{接受劣解}) = \\exp\\!\\Bigl(-\\frac{\\Delta E}{T}\\Bigr),\\qquad T_{k+1} = \\alpha T_k,\\quad 0<\\alpha<1",
    code: {
      python: "import math\nimport random\n\ncurrent, best = x0, x0\nT = T0\nwhile T > T_min:\n    for _ in range(L):\n        candidate = neighbor(current)\n        delta = cost(candidate) - cost(current)\n        if delta < 0 or random.random() < math.exp(-delta / T):\n            current = candidate\n            if cost(current) < cost(best):\n                best = current\n    T *= alpha",
      r: "library(GenSA)\n\nres <- GenSA(par = x0, fn = objective, lower = lb, upper = ub,\n             control = list(maxit = 5000, temperature = 5230, seed = 42))\nres$value\nres$par\nplot(res$trace.mat[, \"function.value\"], type = \"l\")",
      matlab: "options = optimoptions('simulannealbnd', 'InitialTemperature', 100, ...\n    'TemperatureFcn', @temperatureexp, 'PlotFcn', {@saplotbestf, @saplottemperature});\n\n[x, fval, exitflag, output] = simulannealbnd(@objective, x0, lb, ub, options);\nfprintf('接受次数 %d，总迭代 %d\\n', output.totalaccept, output.iterations);",
    },
  },
  "particle-swarm": {
    formula: "v_i \\leftarrow \\omega v_i + c_1 r_1\\bigl(p_i - x_i\\bigr) + c_2 r_2\\bigl(g - x_i\\bigr),\\qquad x_i \\leftarrow x_i + v_i",
    code: {
      python: "import numpy as np\n\nrng = np.random.default_rng(42)\nX = rng.uniform(lb, ub, (n_particles, dim))\nV = np.zeros_like(X)\npbest, pbest_val = X.copy(), np.apply_along_axis(f, 1, X)\ngbest = pbest[pbest_val.argmin()]\nfor t in range(n_iter):\n    w = 0.9 - 0.5 * t / n_iter\n    r1, r2 = rng.random((2, n_particles, dim))\n    V = w * V + 2 * r1 * (pbest - X) + 2 * r2 * (gbest - X)\n    X = np.clip(X + V, lb, ub)",
      r: "library(pso)\n\nres <- psoptim(par = rep(NA, n_var), fn = objective,\n               lower = lb, upper = ub,\n               control = list(maxit = 300, s = 40, w = c(.9, .4),\n                              c.p = 2, c.g = 2, trace = 1))\nres$value; res$par",
      matlab: "options = optimoptions('particleswarm', 'SwarmSize', 40, 'MaxIterations', 300, ...\n    'InertiaRange', [0.4 0.9], 'PlotFcn', @pswplotbestf);\n\n[x, fval, exitflag, output] = particleswarm(@objective, nvars, lb, ub, options);\nfprintf('迭代 %d 次收敛\\n', output.iterations);",
    },
  },
  "ahp": {
    formula: "Aw = \\lambda_{\\max}w,\\qquad \\mathrm{CI}=\\frac{\\lambda_{\\max}-n}{n-1},\\qquad \\mathrm{CR}=\\frac{\\mathrm{CI}}{\\mathrm{RI}} < 0.10",
    code: {
      python: "import numpy as np\nvalues, vectors = np.linalg.eig(A)\nk = np.argmax(values.real)\nw = np.abs(vectors[:, k].real); w /= w.sum()\nci = (values[k].real - len(A)) / (len(A) - 1)\ncr = ci / RI[len(A)]",
      r: "ev <- eigen(A)\nk  <- which.max(Re(ev$values))\nw  <- abs(Re(ev$vectors[, k])); w <- w / sum(w)\n\nlambda_max <- Re(ev$values[k])\nn  <- nrow(A)\nCI <- (lambda_max - n) / (n - 1)\nRI <- c(0, 0, .58, .90, 1.12, 1.24, 1.32, 1.41, 1.45)\nCR <- CI / RI[n]\nstopifnot(CR < 0.10)",
      matlab: "[V, D] = eig(A);\n[lambda_max, k] = max(real(diag(D)));\nw = abs(real(V(:, k))); w = w / sum(w);\n\nn  = size(A, 1);\nCI = (lambda_max - n) / (n - 1);\nRI = [0 0 0.58 0.90 1.12 1.24 1.32 1.41 1.45];\nCR = CI / RI(n);\nassert(CR < 0.10, '判断矩阵一致性不合格，CR=%.4f', CR);",
    },
  },
  "entropy-weight": {
    formula: "e_j = -k\\sum_{i=1}^{m}p_{ij}\\ln p_{ij},\\quad k=\\frac{1}{\\ln m},\\qquad w_j = \\frac{1-e_j}{\\sum_{l=1}^{n}(1-e_l)}",
    code: {
      python: "import numpy as np\nZ = (X - X.min(0)) / (X.max(0) - X.min(0) + 1e-12)\nP = Z / (Z.sum(0) + 1e-12)\nE = -(P * np.log(P + 1e-12)).sum(0) / np.log(len(X))\nw = (1 - E) / (1 - E).sum()",
      r: "Z <- apply(X, 2, function(v) (v - min(v)) / (max(v) - min(v) + 1e-12))\nP <- sweep(Z, 2, colSums(Z) + 1e-12, \"/\")\nE <- -colSums(P * log(P + 1e-12)) / log(nrow(X))\nw <- (1 - E) / sum(1 - E)\nround(w, 4)",
      matlab: "Z = (X - min(X)) ./ (max(X) - min(X) + 1e-12);\nP = Z ./ (sum(Z) + 1e-12);\nE = -sum(P .* log(P + 1e-12)) / log(size(X, 1));\nw = (1 - E) / sum(1 - E);",
    },
  },
  "critic": {
    formula: "C_j = \\sigma_j \\sum_{i=1}^{n}\\bigl(1-r_{ij}\\bigr),\\qquad w_j = \\frac{C_j}{\\sum_{k=1}^{n}C_k}",
    code: {
      python: "import numpy as np\n\nZ = (X - X.min(0)) / (X.max(0) - X.min(0) + 1e-12)\nsigma = Z.std(0, ddof=1)\nR = np.corrcoef(Z, rowvar=False)\nconflict = (1 - R).sum(0)\nC = sigma * conflict\nw = C / C.sum()",
      r: "Z <- apply(X, 2, function(v) (v - min(v)) / (max(v) - min(v) + 1e-12))\nsigma <- apply(Z, 2, sd)\nR <- cor(Z)\nconflict <- colSums(1 - R)\nC <- sigma * conflict\nw <- C / sum(C)",
      matlab: "Z = (X - min(X)) ./ (max(X) - min(X) + 1e-12);\nsigma = std(Z, 0, 1);\nR = corr(Z);\nconflict = sum(1 - R, 1);\nC = sigma .* conflict;\nw = C / sum(C);",
    },
  },
  "topsis": {
    formula: "C_i = \\frac{D_i^-}{D_i^+ + D_i^-},\\qquad D_i^{\\pm} = \\sqrt{\\sum_{j=1}^{n}\\bigl(v_{ij}-v_j^{\\pm}\\bigr)^2},\\quad 0 \\le C_i \\le 1",
    code: {
      python: "import numpy as np\nV = X / np.sqrt((X ** 2).sum(0)) * weights\nbest, worst = V.max(0), V.min(0)\nd_pos = np.linalg.norm(V - best, axis=1)\nd_neg = np.linalg.norm(V - worst, axis=1)\nscore = d_neg / (d_pos + d_neg)",
      r: "V <- sweep(X / sqrt(colSums(X^2)), 2, weights, \"*\")\nbest  <- apply(V, 2, max)\nworst <- apply(V, 2, min)\n\nd_pos <- sqrt(rowSums(sweep(V, 2, best, \"-\")^2))\nd_neg <- sqrt(rowSums(sweep(V, 2, worst, \"-\")^2))\nscore <- d_neg / (d_pos + d_neg)\norder(score, decreasing = TRUE)",
      matlab: "V = X ./ sqrt(sum(X.^2)) .* weights;\nbest  = max(V);\nworst = min(V);\n\nd_pos = sqrt(sum((V - best).^2, 2));\nd_neg = sqrt(sum((V - worst).^2, 2));\nscore = d_neg ./ (d_pos + d_neg);\n[~, rank_idx] = sort(score, 'descend');",
    },
  },
  "grey-relational": {
    formula: "\\xi_i(k) = \\frac{\\Delta_{\\min} + \\rho\\Delta_{\\max}}{\\Delta_i(k) + \\rho\\Delta_{\\max}},\\qquad r_i = \\sum_{k=1}^{n} w_k\\,\\xi_i(k),\\quad \\rho \\in (0,1]",
    code: {
      python: "import numpy as np\n\nZ = X / X.mean(0)              # 均值化无量纲\nref = Z.max(0)                 # 理想参考序列\ndelta = np.abs(Z - ref)\nd_min, d_max, rho = delta.min(), delta.max(), 0.5\nxi = (d_min + rho * d_max) / (delta + rho * d_max)\ngrade = (xi * weights).sum(1)",
      r: "Z <- sweep(X, 2, colMeans(X), \"/\")        # 均值化\nref <- apply(Z, 2, max)\ndelta <- abs(sweep(Z, 2, ref, \"-\"))\n\nd_min <- min(delta); d_max <- max(delta); rho <- .5\nxi <- (d_min + rho * d_max) / (delta + rho * d_max)\ngrade <- as.vector(xi %*% weights)",
      matlab: "Z = X ./ mean(X);                          % 均值化无量纲\nref = max(Z);\ndelta = abs(Z - ref);\n\nd_min = min(delta(:)); d_max = max(delta(:)); rho = 0.5;\nxi = (d_min + rho * d_max) ./ (delta + rho * d_max);\ngrade = xi * weights(:);",
    },
  },
  "fuzzy-evaluation": {
    formula: "B = W \\circ R,\\qquad b_j = \\sum_{i=1}^{m} w_i r_{ij}\\ \\text{(加权平均型)},\\qquad S = \\sum_j b_j v_j",
    code: {
      python: "import numpy as np\n# R[i,j] 为指标 i 对等级 j 的隶属度\nB = weights @ R\nB = B / B.sum()\nlevel = levels[np.argmax(B)]\nscore = B @ level_values",
      r: "B <- as.vector(weights %*% R)\nB <- B / sum(B)\nlevel <- levels_vec[which.max(B)]\nscore <- sum(B * level_values)             # 重心法\ndata.frame(level = levels_vec, membership = round(B, 4))",
      matlab: "B = weights(:)' * R;\nB = B / sum(B);\n[~, k] = max(B);\nlevel = levels(k);\nscore = B * level_values(:);               % 重心法综合得分",
    },
  },
  "shortest-path": {
    formula: "d(v) = \\min_{(u,v) \\in E}\\bigl\\{d(u) + w(u,v)\\bigr\\},\\qquad d(s)=0",
    code: {
      python: "import networkx as nx\nG = nx.DiGraph()\nG.add_weighted_edges_from(edges)\ndistance, path = nx.single_source_dijkstra(G, source, target, weight='weight')",
      r: "library(igraph)\n\ng <- graph_from_data_frame(edges, directed = TRUE)\nres <- shortest_paths(g, from = source, to = target,\n                      weights = E(g)$weight, output = \"both\")\nres$vpath[[1]]\ndistances(g, v = source, to = target, weights = E(g)$weight)",
      matlab: "G = digraph(edges.s, edges.t, edges.w);\n[path, d] = shortestpath(G, source, target);\n\nhighlight(plot(G), path, 'EdgeColor', 'r', 'LineWidth', 2);\nfprintf('最短距离 %.2f\\n', d);",
    },
  },
  "vehicle-routing": {
    formula: "\\min\\sum_{k=1}^{K}\\sum_{(i,j)}c_{ij}x_{ijk} \\quad \\text{s.t.}\\quad \\sum_{i}q_i y_{ik} \\le Q,\\;\\; \\sum_{k}\\sum_{j}x_{ijk}=1\\;\\forall i \\ne 0",
    code: {
      python: "from ortools.constraint_solver import pywrapcp, routing_enums_pb2\n\nmanager = pywrapcp.RoutingIndexManager(n_nodes, n_vehicles, depot)\nrouting = pywrapcp.RoutingModel(manager)\ntransit = routing.RegisterTransitCallback(\n    lambda i, j: dist[manager.IndexToNode(i)][manager.IndexToNode(j)])\nrouting.SetArcCostEvaluatorOfAllVehicles(transit)\nparams = pywrapcp.DefaultRoutingSearchParameters()\nparams.local_search_metaheuristic = (\n    routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH)\nparams.time_limit.seconds = 30\nsolution = routing.SolveWithParameters(params)",
      r: "# 节约算法（Clarke-Wright）构造初始解\nsavings <- outer(1:n, 1:n, Vectorize(function(i, j)\n  if (i < j) dist[1, i+1] + dist[1, j+1] - dist[i+1, j+1] else -Inf))\nord <- which(savings > -Inf, arr.ind = TRUE)\nord <- ord[order(savings[savings > -Inf], decreasing = TRUE), ]\n\nroutes <- lapply(1:n, function(i) i)      # 每客户各成一条路线后逐步合并\n# 依 savings 顺序尝试合并，受容量 Q 约束",
      matlab: "% MATLAB 无内置 VRP，用节约算法 + 2-opt 改进\nS = zeros(n);\nfor i = 1:n\n    for j = i+1:n\n        S(i, j) = D(1, i+1) + D(1, j+1) - D(i+1, j+1);\n    end\nend\n[~, order] = sort(S(:), 'descend');\n\n% 逐条合并并校验容量\nroutes = num2cell(1:n);\nload = demand(:)';\n% ... 合并逻辑，确保 sum(load(route)) <= Q",
    },
  },
  "maximum-flow": {
    formula: "\\max\\;\\lvert f\\rvert \\quad \\text{s.t.}\\quad 0 \\le f(u,v) \\le c(u,v),\\;\\; \\sum_{u}f(u,v)=\\sum_{w}f(v,w)\\;\\forall v \\ne s,t;\\qquad \\lvert f\\rvert_{\\max}=\\min_{\\text{cut}}c(S,\\bar{S})",
    code: {
      python: "import networkx as nx\nvalue, flow = nx.maximum_flow(G, source, sink, capacity='capacity')\ncut_value, partition = nx.minimum_cut(G, source, sink, capacity='capacity')",
      r: "library(igraph)\n\ng <- graph_from_data_frame(edges, directed = TRUE)\nmf <- max_flow(g, source = V(g)[src], target = V(g)[snk],\n               capacity = E(g)$capacity)\nmf$value                                   # 最大流\nmf$partition1                              # 最小割一侧",
      matlab: "G = digraph(edges.s, edges.t, edges.cap);\n[mf, GF, cs, ct] = maxflow(G, source, sink);\n\nfprintf('最大流 %.2f\\n', mf);\ndisp('最小割源侧节点:'); disp(cs);",
    },
  },
  "network-centrality": {
    formula: "C_B(v)=\\sum_{s \\ne v \\ne t}\\frac{\\sigma_{st}(v)}{\\sigma_{st}},\\qquad C_C(v)=\\frac{n-1}{\\sum_{u}d(v,u)},\\qquad A x = \\lambda_{\\max} x",
    code: {
      python: "import networkx as nx\ndegree = nx.degree_centrality(G)\nbetweenness = nx.betweenness_centrality(G, weight='weight')\neigenvector = nx.eigenvector_centrality_numpy(G, weight='weight')",
      r: "library(igraph)\n\ndeg <- degree(g, normalized = TRUE)\nbtw <- betweenness(g, weights = E(g)$weight, normalized = TRUE)\neig <- eigen_centrality(g, weights = E(g)$weight)$vector\n\n# 按介数降序移除节点，观察最大连通分量衰减\nsapply(order(btw, decreasing = TRUE)[1:10], function(v)\n  max(components(delete_vertices(g, v))$csize))",
      matlab: "G = graph(A);\ndeg = centrality(G, 'degree');\nbtw = centrality(G, 'betweenness');\neig = centrality(G, 'eigenvector');\n\n[~, ord] = sort(btw, 'descend');\nfor i = 1:10                               % 逐个移除关键节点\n    H = rmnode(G, ord(1:i));\n    sizes(i) = max(histcounts(conncomp(H)));\nend",
    },
  },
  "seir": {
    formula: "\\frac{\\mathrm{d}S}{\\mathrm{d}t}=-\\frac{\\beta SI}{N},\\quad \\frac{\\mathrm{d}E}{\\mathrm{d}t}=\\frac{\\beta SI}{N}-\\sigma E,\\quad \\frac{\\mathrm{d}I}{\\mathrm{d}t}=\\sigma E-\\gamma I,\\quad \\frac{\\mathrm{d}R}{\\mathrm{d}t}=\\gamma I,\\qquad R_0=\\frac{\\beta}{\\gamma}",
    code: {
      python: "from scipy.integrate import solve_ivp\ndef seir(t, y, beta, sigma, gamma, n):\n    s, e, i, r = y\n    return [-beta*s*i/n, beta*s*i/n-sigma*e, sigma*e-gamma*i, gamma*i]\nsolution = solve_ivp(seir, t_span, y0, args=(beta, sigma, gamma, n), t_eval=t)",
      r: "library(deSolve)\n\nseir <- function(t, y, p) {\n  with(as.list(c(y, p)), {\n    dS <- -beta * S * I / N\n    dE <-  beta * S * I / N - sigma * E\n    dI <-  sigma * E - gamma * I\n    dR <-  gamma * I\n    list(c(dS, dE, dI, dR))\n  })\n}\nout <- ode(y = y0, times = times, func = seir, parms = params)",
      matlab: "seir = @(t, y) [ -beta*y(1)*y(3)/N;\n                  beta*y(1)*y(3)/N - sigma*y(2);\n                  sigma*y(2) - gamma*y(3);\n                  gamma*y(3) ];\n\n[t, y] = ode45(seir, tspan, y0);\nconservation = max(abs(sum(y, 2) - N));    % 人口守恒误差\nplot(t, y); legend('S', 'E', 'I', 'R');",
    },
  },
  "logistic-growth": {
    formula: "\\frac{\\mathrm{d}x}{\\mathrm{d}t}=rx\\Bigl(1-\\frac{x}{K}\\Bigr),\\qquad x(t)=\\frac{K}{1+\\bigl(\\frac{K-x_0}{x_0}\\bigr)e^{-rt}},\\qquad \\text{拐点在 } x=\\frac{K}{2}",
    code: {
      python: "import numpy as np\nfrom scipy.optimize import curve_fit\n\ndef logistic(t, K, r, t0):\n    return K / (1 + np.exp(-r * (t - t0)))\n\nparams, cov = curve_fit(logistic, t_obs, y_obs, p0=[y_obs.max() * 1.5, .3, t_obs.mean()],\n                        bounds=([y_obs.max(), 1e-3, -np.inf], [K_upper, 5, np.inf]))\nK, r, t0 = params",
      r: "fit <- nls(y ~ K / (1 + exp(-r * (t - t0))),\n           data = data.frame(t = t_obs, y = y_obs),\n           start = list(K = max(y_obs) * 1.5, r = .3, t0 = mean(t_obs)),\n           algorithm = \"port\", lower = c(max(y_obs), 1e-3, -Inf))\nsummary(fit)\nconfint(fit)                               # K 的置信区间",
      matlab: "model = @(p, t) p(1) ./ (1 + exp(-p(2) * (t - p(3))));\np0 = [max(y_obs) * 1.5, 0.3, mean(t_obs)];\nlb = [max(y_obs), 1e-3, -inf];\n\n[p, resnorm, residual, ~, ~, ~, J] = lsqcurvefit(model, p0, t_obs, y_obs, lb, []);\nci = nlparci(p, residual, 'jacobian', J);\nfprintf('承载力 K = %.2f，拐点 t0 = %.2f\\n', p(1), p(3));",
    },
  },
  "predator-prey": {
    formula: "\\frac{\\mathrm{d}x}{\\mathrm{d}t}=\\alpha x-\\beta xy,\\qquad \\frac{\\mathrm{d}y}{\\mathrm{d}t}=\\delta xy-\\gamma y,\\qquad \\text{平衡点 }\\Bigl(\\frac{\\gamma}{\\delta},\\frac{\\alpha}{\\beta}\\Bigr)",
    code: {
      python: "from scipy.integrate import solve_ivp\ndef lv(t, z, a, b, d, g):\n    x, y = z\n    return [a*x - b*x*y, d*x*y - g*y]\ntrajectory = solve_ivp(lv, (0, T), initial, args=params, method='Radau', dense_output=True)",
      r: "library(deSolve)\n\nlv <- function(t, z, p) {\n  with(as.list(c(z, p)), list(c(a * x - b * x * y, d * x * y - g * y)))\n}\nout <- ode(y = c(x = x0, y = y0), times = times, func = lv,\n           parms = params, method = \"radau\")\n\n# 平衡点处雅可比特征值判稳定性\nJ <- matrix(c(0, -b * g / d, d * a / b, 0), 2, 2, byrow = TRUE)\neigen(J)$values",
      matlab: "lv = @(t, z) [ a*z(1) - b*z(1)*z(2);\n               d*z(1)*z(2) - g*z(2) ];\n\n[t, z] = ode15s(lv, tspan, [x0; y0]);      % 刚性问题用隐式求解器\n\nxe = g/d; ye = a/b;                        % 平衡点\nJ = [a - b*ye, -b*xe; d*ye, d*xe - g];\neig(J)                                     % 特征值判稳定性",
    },
  },
  "heat-equation": {
    formula: "\\frac{\\partial u}{\\partial t} = \\alpha\\nabla^2 u + q(x,t),\\qquad \\text{显式格式稳定条件 } r=\\frac{\\alpha\\Delta t}{\\Delta x^2} \\le \\frac{1}{2}",
    code: {
      python: "import numpy as np\n# 一维显式有限差分，需满足 r=alpha*dt/dx**2 <= .5\nr = alpha * dt / dx**2\nassert r <= .5, 'unstable: reduce dt or use implicit scheme'\nu_next = u.copy()\nu_next[1:-1] = u[1:-1] + r * (u[2:] - 2*u[1:-1] + u[:-2])",
      r: "r <- alpha * dt / dx^2\nstopifnot(r <= 0.5)                        # 显式格式稳定性\n\nu <- u0\nfor (n in seq_len(nt)) {\n  u_new <- u\n  idx <- 2:(length(u) - 1)\n  u_new[idx] <- u[idx] + r * (u[idx + 1] - 2 * u[idx] + u[idx - 1])\n  u <- u_new\n}",
      matlab: "r = alpha * dt / dx^2;\nassert(r <= 0.5, '显式格式不稳定，需减小 dt 或改隐式');\n\nu = u0;\nfor n = 1:nt\n    u(2:end-1) = u(2:end-1) + r * (u(3:end) - 2*u(2:end-1) + u(1:end-2));\nend\n\n% 或用 pdepe 求解一维抛物型方程\n% sol = pdepe(0, @pdefun, @icfun, @bcfun, xmesh, tspan);",
    },
  },
  "monte-carlo": {
    formula: "\\mathbb{E}[g(X)] \\approx \\frac{1}{N}\\sum_{i=1}^{N}g(X_i),\\qquad \\mathrm{SE} \\approx \\frac{s}{\\sqrt{N}},\\qquad \\text{收敛速率 } O(N^{-1/2})",
    code: {
      python: "import numpy as np\nrng = np.random.default_rng(42)\nsamples = rng.multivariate_normal(mean, covariance, size=100_000)\noutcomes = model(samples)\nsummary = np.quantile(outcomes, [.05, .50, .95])\nstd_err = outcomes.std(ddof=1) / np.sqrt(len(outcomes))",
      r: "set.seed(42)\nlibrary(MASS)\n\nsamples <- mvrnorm(n = 1e5, mu = mu, Sigma = Sigma)\noutcomes <- apply(samples, 1, model)\n\nquantile(outcomes, c(.05, .5, .95))\nse <- sd(outcomes) / sqrt(length(outcomes))",
      matlab: "rng(42);\nsamples = mvnrnd(mu, Sigma, 1e5);\noutcomes = arrayfun(@(i) model(samples(i, :)), 1:size(samples, 1));\n\nq = quantile(outcomes, [0.05 0.5 0.95]);\nse = std(outcomes) / sqrt(numel(outcomes));\n\n% 收敛曲线\nrunning = cumsum(outcomes) ./ (1:numel(outcomes));\nplot(running);",
    },
  },
  "queueing-theory": {
    formula: "\\rho=\\frac{\\lambda}{c\\mu},\\qquad P_{\\text{wait}}=\\frac{\\frac{a^c}{c!}\\frac{c}{c-a}}{\\sum_{k=0}^{c-1}\\frac{a^k}{k!}+\\frac{a^c}{c!}\\frac{c}{c-a}},\\qquad W_q=\\frac{P_{\\text{wait}}}{c\\mu-\\lambda},\\quad a=\\frac{\\lambda}{\\mu}",
    code: {
      python: "import math\n\ndef erlang_c(c: int, a: float) -> float:\n    top = a ** c / math.factorial(c) * c / (c - a)\n    bottom = sum(a ** k / math.factorial(k) for k in range(c)) + top\n    return top / bottom\n\na = lam / mu                 # 提供的负载\nrho = a / c\nassert rho < 1, 'unstable queue'\npw = erlang_c(c, a)\nwq = pw / (c * mu - lam)",
      r: "library(queueing)\n\nm <- NewInput.MMC(lambda = lam, mu = mu, c = c_servers)\nres <- QueueingModel(m)\nsummary(res)\nres$Wq                                     # 平均排队等待时间\nres$Lq; res$RO                             # 队长与利用率",
      matlab: "a = lam / mu;\nrho = a / c;\nassert(rho < 1, '系统不稳定，ρ=%.3f', rho);\n\ntop = a^c / factorial(c) * c / (c - a);\nbottom = sum(a.^(0:c-1) ./ factorial(0:c-1)) + top;\nPw = top / bottom;                         % Erlang C\nWq = Pw / (c * mu - lam);\nLq = lam * Wq;",
    },
  },
  "discrete-event": {
    formula: "t_{\\text{next}} = \\min\\{t_{\\text{arrival}},\\,t_{\\text{departure}},\\,t_{\\text{failure}},\\dots\\},\\qquad \\bar{W}=\\frac{1}{n}\\sum_{i=1}^{n}\\bigl(t_i^{\\text{start}}-t_i^{\\text{arrive}}\\bigr)",
    code: {
      python: "import simpy\ndef customer(env, server, service_time):\n    arrived = env.now\n    with server.request() as request:\n        yield request\n        wait_times.append(env.now - arrived)\n        yield env.timeout(service_time())\nenv = simpy.Environment(); server = simpy.Resource(env, capacity=c)",
      r: "library(simmer)\n\ncustomer <- trajectory() %>%\n  seize(\"server\", 1) %>%\n  timeout(function() rexp(1, mu)) %>%\n  release(\"server\", 1)\n\nenv <- simmer() %>%\n  add_resource(\"server\", capacity = c_servers) %>%\n  add_generator(\"cust\", customer, function() rexp(1, lambda)) %>%\n  run(until = 1000)\n\nget_mon_arrivals(env) %>% transform(wait = end_time - start_time - activity_time)",
      matlab: "% 事件驱动主循环（无需 SimEvents 工具箱）\nt = 0; queue = 0; waits = [];\nt_arr = exprnd(1/lambda); t_dep = inf;\nwhile t < T_end\n    t = min(t_arr, t_dep);\n    if t_arr <= t_dep                      % 到达事件\n        queue = queue + 1;\n        if queue == 1, t_dep = t + exprnd(1/mu); end\n        t_arr = t + exprnd(1/lambda);\n    else                                   % 服务完成事件\n        queue = queue - 1;\n        if queue > 0, t_dep = t + exprnd(1/mu); else, t_dep = inf; end\n    end\nend",
    },
  },
  "cellular-automata": {
    formula: "s_i(t+1) = F\\bigl(s_i(t),\\;\\{s_j(t) : j \\in N(i)\\}\\bigr),\\qquad \\text{全体元胞同步更新}",
    code: {
      python: "import numpy as np\n\n# NaSch 交通流：加速 → 减速 → 随机慢化 → 前进\nv = np.minimum(v + 1, v_max)\ngap = (np.roll(pos, -1) - pos - 1) % road_len\nv = np.minimum(v, gap)\nslow = rng.random(len(v)) < p_slow\nv[slow] = np.maximum(v[slow] - 1, 0)\npos = (pos + v) % road_len",
      r: "# NaSch 交通流：加速 → 减速 → 随机慢化 → 前进\nv <- pmin(v + 1, v_max)\ngap <- (c(pos[-1], pos[1] + road_len) - pos - 1)\nv <- pmin(v, gap)\nslow <- runif(length(v)) < p_slow\nv[slow] <- pmax(v[slow] - 1, 0)\npos <- (pos + v) %% road_len",
      matlab: "% NaSch 交通流模型\nv = min(v + 1, v_max);\ngap = mod(circshift(pos, -1) - pos - 1, road_len);\nv = min(v, gap);\nslow = rand(size(v)) < p_slow;\nv(slow) = max(v(slow) - 1, 0);\npos = mod(pos + v, road_len);\n\nflow(step) = sum(v) / road_len;            % 宏观流量",
    },
  },
  "system-dynamics": {
    formula: "\\mathrm{Stock}(t+\\Delta t) = \\mathrm{Stock}(t) + \\bigl[\\mathrm{Inflow}(t) - \\mathrm{Outflow}(t)\\bigr]\\Delta t",
    code: {
      python: "import numpy as np\nfor t in range(steps - 1):\n    inflow = adoption_rate(stock[t], policy[t])\n    outflow = retirement_rate(stock[t])\n    stock[t + 1] = stock[t] + (inflow - outflow) * dt",
      r: "library(deSolve)\n\nsd_model <- function(t, y, p) {\n  with(as.list(c(y, p)), {\n    inflow  <- adoption_rate * stock * (1 - stock / capacity)\n    outflow <- retire_rate * stock\n    list(c(stock = inflow - outflow))\n  })\n}\nout <- ode(y = c(stock = s0), times = seq(0, T, by = dt),\n           func = sd_model, parms = params)",
      matlab: "stock = zeros(1, steps); stock(1) = s0;\nfor t = 1:steps-1\n    inflow  = adoption_rate * stock(t) * (1 - stock(t) / capacity);\n    outflow = retire_rate * stock(t);\n    stock(t+1) = stock(t) + (inflow - outflow) * dt;\nend\n\n% 时间步减半复算，验证数值收敛\nplot((0:steps-1) * dt, stock);",
    },
  },
  "agent-based": {
    formula: "\\mathrm{state}_i(t+1) = F\\bigl(\\mathrm{state}_i(t),\\;\\mathcal{N}_i(t),\\;\\mathrm{env}(t),\\;\\xi_{it}\\bigr)",
    code: {
      python: "from mesa import Agent, Model\nclass Person(Agent):\n    def step(self):\n        neighbors = self.model.grid.get_neighbors(self.pos, moore=True)\n        self.state = decision_rule(self.state, neighbors, self.random)\nmodel.step()  # 调度全部智能体一次",
      r: "library(NetLogoR)\n\nagents <- createTurtles(n = 500, world = world, heading = runif(500, 0, 360))\nfor (step in 1:1000) {\n  agents <- agents[sample(NLcount(agents)), ]   # 打乱调度顺序\n  neighbors <- NLwith(agents, world, radius = 2)\n  agents <- NLset(turtles = agents, agents = agents,\n                  var = \"state\", val = decision_rule(agents, neighbors))\n}",
      matlab: "for step = 1:n_steps\n    order = randperm(n_agents);            % 每步随机打乱调度顺序\n    for k = order\n        nb = find(pdist2(pos(k, :), pos) < radius);\n        state(k) = decision_rule(state(k), state(nb));\n    end\n    macro(step) = mean(state);             % 宏观指标\nend",
    },
  },
  "sensitivity-analysis": {
    formula: "S_i = \\frac{\\operatorname{Var}\\bigl[\\mathbb{E}(Y \\mid X_i)\\bigr]}{\\operatorname{Var}(Y)},\\qquad S_{T_i} = 1 - \\frac{\\operatorname{Var}\\bigl[\\mathbb{E}(Y \\mid X_{\\sim i})\\bigr]}{\\operatorname{Var}(Y)}",
    code: {
      python: "from SALib.sample import saltelli\nfrom SALib.analyze import sobol\n\nproblem = {'num_vars': 3, 'names': ['beta', 'gamma', 'k'],\n           'bounds': [[.1, .8], [.05, .4], [100, 500]]}\nX = saltelli.sample(problem, 1024)\nY = np.array([model(*row) for row in X])\nSi = sobol.analyze(problem, Y, print_to_console=True)\nfirst_order, total = Si['S1'], Si['ST']",
      r: "library(sensitivity)\n\nx1 <- data.frame(beta = runif(1024, .1, .8), gamma = runif(1024, .05, .4))\nx2 <- data.frame(beta = runif(1024, .1, .8), gamma = runif(1024, .05, .4))\nsa <- sobolSalt(model = model_fun, X1 = x1, X2 = x2, scheme = \"A\", nboot = 100)\nprint(sa); plot(sa)                        # 含 Bootstrap 置信区间",
      matlab: "% 用 Saltelli 抽样估计一阶与总效应指数\nN = 1024;\nA = lb + (ub - lb) .* rand(N, k);\nB = lb + (ub - lb) .* rand(N, k);\nyA = arrayfun(@(i) model(A(i, :)), 1:N)';\nyB = arrayfun(@(i) model(B(i, :)), 1:N)';\n\nfor j = 1:k\n    AB = A; AB(:, j) = B(:, j);\n    yAB = arrayfun(@(i) model(AB(i, :)), 1:N)';\n    S1(j) = mean(yB .* (yAB - yA)) / var([yA; yB]);\n    ST(j) = mean((yA - yAB).^2) / (2 * var([yA; yB]));\nend",
    },
  },
  "model-validation": {
    formula: "D_i = \\frac{\\sum_{j=1}^{n}\\bigl(\\hat{y}_j - \\hat{y}_{j(i)}\\bigr)^2}{p \\cdot \\mathrm{MSE}},\\qquad D_i > \\frac{4}{n} \\text{ 视为强影响点}",
    code: {
      python: "import numpy as np\nimport statsmodels.api as sm\nfrom sklearn.model_selection import cross_val_score\n\ncv = cross_val_score(model, X, y, cv=10, scoring='neg_root_mean_squared_error')\nprint(f'CV RMSE = {-cv.mean():.4f} ± {cv.std():.4f}')\n\ninfluence = sm.OLS(y, sm.add_constant(X)).fit().get_influence()\ncooks = influence.cooks_distance[0]\nflagged = np.where(cooks > 4 / len(y))[0]",
      r: "library(caret)\n\ncv <- train(y ~ ., data = df, method = \"lm\",\n            trControl = trainControl(method = \"cv\", number = 10))\nprint(cv$results)\n\nfit <- lm(y ~ ., data = df)\ncooks <- cooks.distance(fit)\nwhich(cooks > 4 / nrow(df))                # 强影响点\npar(mfrow = c(2, 2)); plot(fit)",
      matlab: "cv = cvpartition(numel(y), 'KFold', 10);\nrmse = zeros(cv.NumTestSets, 1);\nfor i = 1:cv.NumTestSets\n    mdl = fitlm(X(training(cv, i), :), y(training(cv, i)));\n    pred = predict(mdl, X(test(cv, i), :));\n    rmse(i) = sqrt(mean((pred - y(test(cv, i))).^2));\nend\nfprintf('CV RMSE = %.4f ± %.4f\\n', mean(rmse), std(rmse));\n\nmdl = fitlm(X, y);\nflagged = find(mdl.Diagnostics.CooksDistance > 4 / numel(y));",
    },
  },
};
